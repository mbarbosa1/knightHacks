'''
Tools the detect-agent calls instead of writing SQL (see detect-agent.md)

detect_tools.py  One function per tool in detect-agent.md. Each function takes an open
database `connection` logged in as the `agents` role (AGENTS_DATABASE_URL), plus the
`run_id` of the current event. The agent itself never sees `run_id`: the orchestrator
binds it (e.g. functools.partial) when it registers the tools for one event, so the
model can't read or write another run.

Plan:
  - Reuse backend/app/repository.py where it already does the job (list_observations,
    put_incident, put_road_restriction, require_run, writable_run). Those functions
    validate input and append to ops.operational_events, so the outbox stays correct.
  - repository.py raises HTTPException on bad input (404 / 409 / 422). Catch it here and
    return {"error": <message>} so the model can read it and retry, instead of crashing.
  - Neon connection errors -> return {"error": "neon_unavailable"} (detect-agent.md, Rules).
  - Return plain JSON-safe dicts/lists (UUIDs and datetimes as strings, geometry as
    {"longitude", "latitude"}); the model reads these directly.
  - Write tools run in one transaction each.

agents role permissions (database/schema.sql):
  read:          static.*, ops.simulation_runs, ops.observations, ops.resources
  read + write:  ops.incidents, ops.road_restrictions, ops.agent_runs
  insert only:   ops.operational_events

Sections in this file:
  1. Event          (get_run, get_last_agent_run)
  2. Reads          (list_observations, list_incidents)
  3. Spatial lookup (find_nearby_incidents, find_nearby_buildings, find_nearest_road)
  4. Writes         (upsert_incident, set_road_restriction, log_agent_run)
'''


# ============================================================================
# 1. Event
# ============================================================================

async def get_run(connection, run_id):
    """Return the current event, or {"error": "run_not_found"}.

    Reads ops.simulation_runs: id, map_version_id, hazard_types, status, started_at,
    ended_at. Also return a derived `"live": bool` (False when ended_at is set or status
    is 'completed'/'failed') so the agent knows whether it's in read-only mode.
    Can wrap repository.require_run.
    """
    raise NotImplementedError


async def get_last_agent_run(connection, run_id, agent="detect-agent"):
    """Return this agent's most recent ops.agent_runs row for the run, or None.

    Fields: id, created_at, input_ref (the cursor it stopped at), output (its last JSON
    hand-off). The agent passes created_at to list_incidents(updated_since=...) to see
    what changed since it last looked.
    """
    raise NotImplementedError


# ============================================================================
# 2. Reads
# ============================================================================

async def list_observations(connection, run_id, after=None, ids=None, limit=100):
    """Return observations for the run, ordered by (received_at, id).

    - ids given:   only those observations (ignore `after`); skip IDs not in this run.
    - after given: only observations after that cursor (same rule as
                   repository.list_observations).
    - neither:     from the start of the run.
    Fields: id, channel, source_reliability, format, lang, original_text, text_en,
    claimed_location, location_precision, location {longitude, latitude} | None,
    received_at, sim_time_s.
    Decide how to page when there are more than `limit` (e.g. return "next_cursor").
    """
    raise NotImplementedError


async def list_incidents(connection, run_id, updated_since=None):
    """Return the run's incidents, newest updated_at first.

    updated_since given -> only incidents with updated_at > updated_since (used to trace
    what other writers changed). Fields: id, status, building_id, person_groups,
    evidence, uncertainty_m, location {longitude, latitude} | None, created_at,
    updated_at. Same columns as GET /runs/{run_id}/incidents in backend/app/main.py.
    """
    raise NotImplementedError


# ============================================================================
# 3. Spatial lookup
# ============================================================================

async def find_nearby_incidents(connection, run_id, lon, lat, radius_m):
    """Return incidents in this run within radius_m of (lon, lat), nearest first.

    Use public.ST_DWithin on geography so radius_m is in metres. Each result carries
    `distance_m` plus the same fields as list_incidents. Incidents with no geom are skipped.
    Consider capping radius_m (e.g. 2000) so a bad call can't return the whole run.
    """
    raise NotImplementedError


async def find_nearby_buildings(connection, run_id, lon, lat, radius_m, limit=3):
    """Return static.buildings near (lon, lat) in the run's map version, nearest first.

    Look up map_version_id from ops.simulation_runs; the agent never passes it.
    Fields the agent's predictions need: id, name_en, usage, structure, era, era_probs,
    floors, height_m, year_built, soil_amp, facility_type, distance_m, and whether the
    point is inside the footprint (ST_Contains).
    repository.get_building_context may already cover part of this.
    """
    raise NotImplementedError


async def find_nearest_road(connection, run_id, lon, lat, max_distance_m=200):
    """Return the static.road_segments row nearest to (lon, lat), or None.

    Look up map_version_id from the run. Fields: id (the road_id that
    set_road_restriction expects), name_en, name_ja, length_m, distance_m, and the
    road's current ops.road_restrictions status for this run if one exists.
    None when nothing is within max_distance_m.
    """
    raise NotImplementedError


# ============================================================================
# 4. Writes
# ============================================================================

async def upsert_incident(connection, run_id, incident_id=None, location=None, uncertainty_m=None,
                          status="reported", building_id=None, person_groups=None,
                          evidence=None, sim_time_s=None):
    """Create or fully replace an incident; return the saved row.

    - incident_id None -> generate a new UUID (create). Otherwise update that incident.
    - Full replace, not a merge: the agent sends the whole new state.
    - Build a models.IncidentPut from the arguments and call repository.put_incident,
      which checks the run is still live, checks building_id and evidence observation
      IDs against this run, rejects duplicate group_ids, and writes the outbox row.
    - Return the row plus `"change": "created" | "updated" | "unchanged"`.
    - Validation failures -> {"error": ...}.
    """
    raise NotImplementedError


async def set_road_restriction(connection, run_id, road_id, status, source_observation_id=None,
                               sim_time_s=None):
    """Set one road's status (OPEN / LIMITED / BLOCKED) for this run; return the row.

    Build a models.RoadRestrictionPut and call repository.put_road_restriction (checks
    road_id against the run's map version and the source observation against this run,
    writes the outbox row). Validation failures -> {"error": ...}.
    """
    raise NotImplementedError


async def log_agent_run(connection, run_id, input_ref, output, duration_ms=None, agent="detect-agent"):
    """Insert one ops.agent_runs row for this invocation; return its id.

    input_ref = last observation ID processed (the next cursor), output = the agent's
    hand-off JSON. Called on every invocation, even when nothing changed.
    duration_ms is better measured by the orchestrator than by the model; consider
    filling it in there and not exposing it as a tool argument.
    """
    raise NotImplementedError
