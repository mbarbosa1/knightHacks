'''
All the SQL lives here

app/repository.py  Every function takes an open database `connection` (handed out by
db.get_connection, one per request) and runs queries against Neon. main.py routes
call these functions; models.py has already validated the request body by the time
it gets here.

Two schemas in the database (see database/schema.sql, docs: database/final-database.md):
  static.*  the map: buildings, facilities, road_segments, map_versions.
            Read-only from the API's point of view.
  ops.*     things that happen during a simulation run: simulation_runs,
            observations, resources, incidents, road_restrictions, and the
            operational_events log.

Things you will see over and over:
  - `await (await connection.execute(...)).fetchone()`: run the query, then read
    one row. Rows come back as dicts (row_factory=dict_row in db.py).
  - `%s` placeholders: psycopg fills them in safely from the tuple of params.
    Never build SQL with f-strings.
  - `public.ST_*`: PostGIS geometry functions (points, distances, GeoJSON).
  - raising HTTPException: stops the request and sends that status code to the
    client. Because the whole request is one transaction, anything written
    before the error is rolled back.

Sections in this file:
  1. Lookup helpers           (fetch a map/run or 404)
  2. Static map reads         (facilities, buildings, GeoJSON layers)
  3. Simulation run lifecycle (create, check writable, end)
  4. Observations             (incoming reports from the simulator)
  5. Validation helpers       (make sure IDs belong to the right run/map)
  6. Live state upserts       (resources, incidents, road restrictions)
  7. Event log                (append-only history of every change)
'''

import math
import struct

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg import sql
from psycopg.types.json import Jsonb

from .models import ObservationCreate, RunCreate

# Tables follow database/schema.sql (docs: database/final-database.md).


# =============================================================================
# 1. LOOKUP HELPERS
#    Small "get it or 404" functions that other functions call first.
# =============================================================================

async def require_map(connection, version_id):
    """Return the map version row, or 404 if that version doesn't exist."""
    row = await (await connection.execute(
        "SELECT id, created_at, source_versions, notes FROM static.map_versions WHERE id=%s",
        (version_id,),
    )).fetchone()
    if row is None:
        raise HTTPException(404, "Map version not found")
    return row


async def require_run(connection, run_id, lock=False):
    """Return the simulation run row, or 404 if it doesn't exist.

    lock=True adds FOR UPDATE, which locks the row until this request's
    transaction finishes. Use it before writing, so two requests can't
    change the same run at the same time (e.g. one ending it while another
    adds an observation).
    """
    query = "SELECT id, map_version_id, hazard_types, status, started_at, ended_at FROM ops.simulation_runs WHERE id=%s"
    if lock:
        query += " FOR UPDATE"
    row = await (await connection.execute(query, (run_id,))).fetchone()
    if row is None:
        raise HTTPException(404, "Run not found")
    return row


# =============================================================================
# 2. STATIC MAP READS
#    Read-only queries against the static.* tables for one map version.
# =============================================================================

async def list_facilities(connection, version_id):
    """All facilities (hospitals, shelters, stations...) in a map version.

    geom is converted to GeoJSON so the frontend can draw it directly.
    """
    await require_map(connection, version_id)
    return await (await connection.execute(
        '''SELECT id, type, subtype, name_en, name_ja, building_id, capacity, access_node,
                  public.ST_AsGeoJSON(geom)::jsonb AS geometry
           FROM static.facilities WHERE map_version_id=%s ORDER BY type, id''',
        (version_id,),
    )).fetchall()


async def get_building_context(connection, version_id, building_id, radius_m, limit):
    """One building's details plus the facilities within radius_m metres of it.

    Returns {"building": {...}, "nearby_facilities": [...]}, nearest first.
    """
    await require_map(connection, version_id)

    # Step 1: the building itself (404 if it isn't in this map version).
    building = await (await connection.execute(
        '''SELECT id, map_version_id, map_id, height_m, floors, basement_floors, footprint_m2,
                  structure, fire_resistance, fireproof_code, year_built, era, era_probs,
                  usage, soil_amp, facility_type, facility_id, name_en, name_ja,
                  address_en, address_ja,
                  public.ST_AsGeoJSON(geom)::jsonb AS geometry
           FROM static.buildings WHERE map_version_id=%s AND id=%s''',
        (version_id, building_id),
    )).fetchone()
    if building is None:
        raise HTTPException(404, "Building not found")

    # Step 2: nearby facilities.
    # - ST_PointOnSurface(b.geom): a single point guaranteed inside the building outline.
    # - ::geography: casting to geography makes distances come out in metres
    #   instead of degrees of longitude/latitude.
    # - ST_DWithin filters to within radius_m; ST_Distance gives the exact distance
    #   so we can sort by it.
    facilities = await (await connection.execute(
        '''SELECT f.id, f.type, f.subtype, f.name_en, f.name_ja, f.building_id, f.capacity,
                  f.access_node, public.ST_AsGeoJSON(f.geom)::jsonb AS geometry,
                  public.ST_Distance(f.geom::public.geography,
                      public.ST_PointOnSurface(b.geom)::public.geography) AS distance_m
           FROM static.facilities f
           JOIN static.buildings b ON b.map_version_id=f.map_version_id
           WHERE b.map_version_id=%s AND b.id=%s
             AND public.ST_DWithin(f.geom::public.geography,
                 public.ST_PointOnSurface(b.geom)::public.geography, %s)
           ORDER BY distance_m, f.id LIMIT %s''',
        (version_id, building_id, radius_m, limit),
    )).fetchall()
    return {"building": building, "nearby_facilities": facilities}


async def bulk_map(connection, version_id, layer, bbox=None):
    """A whole map layer ("buildings" or "road_segments") as a GeoJSON FeatureCollection.

    Optional bbox (west, south, east, north) limits it to features touching that box.
    """
    await require_map(connection, version_id)

    # The table name is inserted into the SQL (it can't be a %s placeholder),
    # so only allow these two known names.
    if layer not in {"buildings", "road_segments"}:
        raise ValueError("Unknown map layer")

    # "properties" = every column of the row as JSON, minus the geometry
    # (geometry goes in its own GeoJSON field).
    # Geometry stays separate from properties; unknown heights stay JSON null.
    properties = sql.SQL("to_jsonb(t) - 'geom'")
    if layer == "road_segments":
        # Node IDs are sent as strings so big integers don't lose precision in JavaScript.
        properties = sql.SQL("(to_jsonb(t) - 'geom') || jsonb_build_object('from_node', t.from_node::text, 'to_node', t.to_node::text)")

    # Build the WHERE clause; add the bounding-box filter only if one was given.
    where = sql.SQL("t.map_version_id=%s")
    params = [version_id]
    if bbox is not None:
        where += sql.SQL(" AND public.ST_Intersects(t.geom, public.ST_MakeEnvelope(%s,%s,%s,%s,4326))")
        params.extend(bbox)

    # sql.Identifier safely quotes the table name.
    rows = await (await connection.execute(sql.SQL(
        "SELECT t.id, public.ST_AsGeoJSON(t.geom)::jsonb AS geometry, {} AS properties "
        "FROM static.{} t WHERE {} ORDER BY t.id"
    ).format(properties, sql.Identifier(layer), where), params)).fetchall()

    # Wrap the rows in the standard GeoJSON shape that map libraries expect.
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": r["id"], "geometry": r["geometry"], "properties": r["properties"]}
        for r in rows
    ]}


def parse_bbox(value):
    """Turn the ?bbox=west,south,east,north query string into a tuple of 4 floats.

    Returns None if no bbox was given. Sends a 422 if it's malformed, not finite,
    out of longitude/latitude range, or west/south aren't smaller than east/north.
    """
    if value is None:
        return None
    try:
        bounds = tuple(float(v) for v in value.split(","))
        west, south, east, north = bounds  # fails (ValueError) unless exactly 4 numbers
        if not all(math.isfinite(v) for v in bounds):
            raise ValueError
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError
        return bounds
    except (TypeError, ValueError):
        raise HTTPException(422, "bbox must be west,south,east,north in longitude/latitude")


# =============================================================================
# 3. SIMULATION RUN LIFECYCLE
#    A run goes: created -> running (on first observation) -> completed | failed.
#    Once ended, nothing else can be written to it.
# =============================================================================

async def create_run(connection, body: RunCreate):
    """Start a new simulation run on a map version. Status starts as 'created'."""
    await require_map(connection, body.map_version_id)
    # The seed is deliberately absent from the returned run context.
    row = await (await connection.execute(
        '''INSERT INTO ops.simulation_runs (map_version_id, seed, hazard_types, status, started_at)
           VALUES (%s,%s,%s,'created',now())
           RETURNING id, map_version_id, hazard_types, status, started_at, ended_at''',
        (body.map_version_id, body.seed, body.hazard_types),
    )).fetchone()
    await record_event(connection, row["id"], "simulation_runs", row["id"], "INSERT", row)
    return row


async def writable_run(connection, run_id):
    """Lock the run and return it, or 409 if it has already ended.

    Every write to a run's live state (section 6) calls this first.
    """
    run = await require_run(connection, run_id, lock=True)
    if run["ended_at"] is not None or run["status"] in {"completed", "failed"}:
        raise HTTPException(409, "Run has ended")
    return run


async def end_run(connection, run_id, body):
    """Mark a run as completed or failed and stamp ended_at.

    Safe to retry: ending an already-ended run with the same status just
    returns it. A different status gets a 409.
    """
    run = await require_run(connection, run_id, lock=True)

    # Already ended? Same status = harmless retry; different status = conflict.
    if run["ended_at"] is not None or run["status"] in {"completed", "failed"}:
        if run["status"] == body.status and run["ended_at"] is not None:
            return run
        raise HTTPException(409, "Run already ended with a different status")

    await connection.execute(
        "UPDATE ops.simulation_runs SET status=%s, ended_at=now() WHERE id=%s",
        (body.status, run_id),
    )
    row = await require_run(connection, run_id)
    await record_event(connection, run_id, "simulation_runs", run_id, "UPDATE", row, body.sim_time_s)
    return row


# =============================================================================
# 4. OBSERVATIONS
#    Raw reports coming in during a run (119 calls, social media, sensors...).
#    They are insert-only: once saved, an observation never changes.
# =============================================================================

async def get_observation(connection, run_id, observation_id):
    """One observation, or None if not found.

    The stored point is split back into longitude/latitude columns.
    """
    return await (await connection.execute(
        '''SELECT id, run_id, channel, format, original_text, lang, text_en,
                  media_url, claimed_location, location_precision, source_reliability,
                  received_at, sim_time_s,
                  public.ST_X(claimed_geom) AS longitude, public.ST_Y(claimed_geom) AS latitude
           FROM ops.observations WHERE run_id=%s AND id=%s''',
        (run_id, observation_id),
    )).fetchone()


async def save_observation(connection, run_id, body: ObservationCreate):
    """Store a new observation for a run.

    Steps:
      1. Lock the run.
      2. If the client sent an ID we've already saved, treat it as a retry
         (return the existing row if identical, 409 if different).
      3. Refuse if the run has ended.
      4. Insert the row and log it.
      5. If this is the run's first observation, flip status created -> running.
    """
    # 1. Lock the run.
    run = await require_run(connection, run_id, lock=True)

    # 2. Retry check (idempotency).
    if body.id is not None:
        existing = await get_observation(connection, run_id, body.id)
        if existing:
            # A retry with the same ID returns the stored row instead of a duplicate.
            if not observation_matches(existing, body):
                raise HTTPException(409, "Observation ID already exists with a different payload")
            return existing

    # 3. No writes after the run has ended.
    if run["status"] in {"completed", "failed"} or run["ended_at"] is not None:
        raise HTTPException(409, "Run has ended")

    # 4. Insert.
    # - COALESCE(%s, gen_random_uuid()): use the client's ID, or make one up.
    # - The CASE builds a PostGIS point (SRID 4326 = normal GPS lon/lat)
    #   only if a location was sent; otherwise geom is NULL. That's why
    #   longitude appears twice in the params: once for the NULL check, once
    #   for ST_MakePoint.
    location = body.location
    row = await (await connection.execute(
        '''INSERT INTO ops.observations
           (id,run_id,channel,format,original_text,lang,text_en,media_url,
            claimed_location,claimed_geom,location_precision,source_reliability,sim_time_s)
           VALUES (COALESCE(%s, gen_random_uuid()),%s,%s::ops.observation_channel,%s,%s,%s,%s,%s,%s,
             CASE WHEN %s::double precision IS NULL THEN NULL ELSE
               public.ST_SetSRID(public.ST_MakePoint(%s,%s),4326) END,%s,%s,%s)
           RETURNING id''',
        (body.id, run_id, body.channel, body.format, body.original_text, body.lang,
         body.text_en, body.media_url, body.claimed_location,
         location.longitude if location else None,
         location.longitude if location else None, location.latitude if location else None,
         body.location_precision, body.source_reliability, body.sim_time_s),
    )).fetchone()
    await record_event(connection, run_id, "observations", row["id"], "INSERT",
                       await get_observation(connection, run_id, row["id"]), body.sim_time_s)

    # 5. First observation starts the run.
    if run["status"] == "created":
        await connection.execute("UPDATE ops.simulation_runs SET status='running' WHERE id=%s", (run_id,))
        await record_event(connection, run_id, "simulation_runs", run_id, "UPDATE",
                           await require_run(connection, run_id), body.sim_time_s)
    return await get_observation(connection, run_id, row["id"])


def observation_matches(existing, body):
    """True if a stored observation has the same content as the incoming request.

    Used by save_observation to tell a harmless retry from a conflicting one.
    """
    # Flatten the request into the same shape as the DB row
    # (location becomes separate longitude/latitude keys).
    expected = body.model_dump(exclude={"location", "id"})
    expected.update(body.location.model_dump() if body.location else {"longitude": None, "latitude": None})
    for key, value in expected.items():
        stored = existing[key]
        if key in {"source_reliability", "sim_time_s"} and value is not None and stored is not None:
            # These columns are Postgres REAL (float32), not arbitrary precision.
            # e.g. 0.1 comes back as 0.10000000149..., so compare both values
            # after rounding them to float32 bytes.
            try:
                stored = struct.pack("!f", stored)
                value = struct.pack("!f", value)
            except (OverflowError, struct.error):
                return False
        if stored != value:
            return False
    return True


# =============================================================================
# 5. VALIDATION HELPERS
#    Make sure IDs in a request point to things that belong to this run
#    (or this run's map version). Bad references get a 422.
# =============================================================================

async def check_static(connection, run, table, identifier):
    """422 unless `identifier` exists in static.<table> for the run's map version.

    Does nothing if identifier is None (the reference is optional).
    """
    if identifier is None:
        return
    # Table name is put into the SQL directly, so only allow known tables.
    if table not in {"buildings", "facilities", "road_segments"}:
        raise ValueError("Unknown static table")
    row = await (await connection.execute(sql.SQL(
        "SELECT id FROM static.{} WHERE map_version_id=%s AND id=%s"
    ).format(sql.Identifier(table)), (run["map_version_id"], identifier))).fetchone()
    if row is None:
        raise HTTPException(422, f"{table} reference does not belong to the run's map version")


async def check_observations(connection, run_id, ids):
    """422 unless every observation ID in `ids` belongs to this run."""
    if not ids:
        return
    # One query for all IDs; set() removes duplicates before comparing.
    found = await (await connection.execute(
        "SELECT id FROM ops.observations WHERE run_id=%s AND id=ANY(%s::uuid[])",
        (run_id, list(set(ids))),
    )).fetchall()
    if {r["id"] for r in found} != set(ids):
        raise HTTPException(422, "Evidence/source observations must belong to this run")


# =============================================================================
# 6. LIVE STATE UPSERTS
#    The agents' current picture of the world during a run. Each PUT either
#    creates the row or overwrites it ("upsert").
#
#    All three functions follow the same pattern:
#      a. writable_run()  - lock the run, 409 if it has ended
#      b. check_*()       - validate any referenced IDs
#      c. INSERT ... ON CONFLICT DO UPDATE ... WHERE ... IS DISTINCT FROM ...
#           The WHERE means "only update if something actually changed".
#           If nothing changed, Postgres returns no row (row is None).
#      d. row is None -> re-read the existing row and return it, no event logged
#         row returned -> something changed, so record_event()
#    This makes repeated identical PUTs harmless and keeps the event log free
#    of no-op entries.
# =============================================================================

async def put_resource(connection, run_id, resource_id, body):
    """Create or update a resource (ambulance, fire truck, team...) in a run."""
    run = await writable_run(connection, run_id)
    await check_static(connection, run, "facilities", body.home_facility_id)
    location = body.location
    # Extra condition in WHERE: ops.resources.run_id=EXCLUDED.run_id stops a
    # resource ID from one run being overwritten by a different run.
    row = await (await connection.execute(
        '''INSERT INTO ops.resources (id,run_id,type,name,geom,home_facility_id,status,capacity,skills)
           VALUES (%s,%s,%s,%s,CASE WHEN %s::double precision IS NULL THEN NULL ELSE
             public.ST_SetSRID(public.ST_MakePoint(%s,%s),4326) END,%s,%s,%s,%s)
           ON CONFLICT (id) DO UPDATE SET type=EXCLUDED.type, name=EXCLUDED.name,
             geom=EXCLUDED.geom, home_facility_id=EXCLUDED.home_facility_id,
             status=EXCLUDED.status, capacity=EXCLUDED.capacity, skills=EXCLUDED.skills
           WHERE ops.resources.run_id=EXCLUDED.run_id
             AND (ops.resources.type,ops.resources.name,ops.resources.geom,
                  ops.resources.home_facility_id,ops.resources.status,ops.resources.capacity,ops.resources.skills)
               IS DISTINCT FROM (EXCLUDED.type,EXCLUDED.name,EXCLUDED.geom,
                  EXCLUDED.home_facility_id,EXCLUDED.status,EXCLUDED.capacity,EXCLUDED.skills)
           RETURNING id,run_id,type,name,home_facility_id,status,capacity,skills,
                     public.ST_AsGeoJSON(geom)::jsonb AS location''',
        (resource_id, run_id, body.type, body.name, location.longitude if location else None,
         location.longitude if location else None, location.latitude if location else None,
         body.home_facility_id, body.status, body.capacity, body.skills),
    )).fetchone()
    if row is None:
        # Nothing was written: either it was unchanged (return it as-is)
        # or the ID belongs to another run (not found here -> 409).
        row = await (await connection.execute(
            '''SELECT id,run_id,type,name,home_facility_id,status,capacity,skills,
                      public.ST_AsGeoJSON(geom)::jsonb AS location
               FROM ops.resources WHERE run_id=%s AND id=%s''', (run_id, resource_id),
        )).fetchone()
        if row is None:
            raise HTTPException(409, "Resource ID is already assigned to another run")
    else:
        await record_event(connection, run_id, "resources", resource_id, "UPSERT", row, body.sim_time_s)
    return row


async def put_incident(connection, run_id, incident_id, body):
    """Create or update an incident (e.g. collapsed building, fire, trapped people) in a run.

    evidence.supports / evidence.contradicts are observation IDs from this run.
    person_groups is stored as JSON.
    """
    run = await writable_run(connection, run_id)
    await check_static(connection, run, "buildings", body.building_id)
    await check_observations(connection, run_id, body.evidence.supports + body.evidence.contradicts)

    # Each person group inside one incident needs its own group_id.
    group_ids = [g.group_id for g in body.person_groups or []]
    if len(group_ids) != len(set(group_ids)):
        raise HTTPException(422, "Person group IDs must be unique within an incident")

    location = body.location
    # Same upsert pattern as put_resource; also bumps updated_at when something changes.
    # Jsonb(...) wraps Python lists/dicts so psycopg sends them as JSONB.
    row = await (await connection.execute(
        '''INSERT INTO ops.incidents (id,run_id,geom,uncertainty_m,status,building_id,person_groups,evidence)
           VALUES (%s,%s,CASE WHEN %s::double precision IS NULL THEN NULL ELSE
             public.ST_SetSRID(public.ST_MakePoint(%s,%s),4326) END,%s,%s,%s,%s,%s)
           ON CONFLICT (id) DO UPDATE SET geom=EXCLUDED.geom, uncertainty_m=EXCLUDED.uncertainty_m,
             status=EXCLUDED.status, building_id=EXCLUDED.building_id,
             person_groups=EXCLUDED.person_groups, evidence=EXCLUDED.evidence, updated_at=now()
           WHERE ops.incidents.run_id=EXCLUDED.run_id
             AND (ops.incidents.geom,ops.incidents.uncertainty_m,ops.incidents.status,
                  ops.incidents.building_id,ops.incidents.person_groups,ops.incidents.evidence)
               IS DISTINCT FROM (EXCLUDED.geom,EXCLUDED.uncertainty_m,EXCLUDED.status,
                  EXCLUDED.building_id,EXCLUDED.person_groups,EXCLUDED.evidence)
           RETURNING id,run_id,uncertainty_m,status,building_id,person_groups,evidence,
                     created_at,updated_at,public.ST_AsGeoJSON(geom)::jsonb AS location''',
        (incident_id, run_id, location.longitude if location else None,
         location.longitude if location else None, location.latitude if location else None,
         body.uncertainty_m, body.status, body.building_id,
         Jsonb(body.model_dump(mode="json")["person_groups"]) if body.person_groups is not None else None,
         Jsonb(body.evidence.model_dump(mode="json"))),
    )).fetchone()
    if row is None:
        # Unchanged -> return existing row; belongs to another run -> 409.
        row = await (await connection.execute(
            '''SELECT id,run_id,uncertainty_m,status,building_id,person_groups,evidence,
                      created_at,updated_at,public.ST_AsGeoJSON(geom)::jsonb AS location
               FROM ops.incidents WHERE run_id=%s AND id=%s''', (run_id, incident_id),
        )).fetchone()
        if row is None:
            raise HTTPException(409, "Incident ID is already assigned to another run")
    else:
        await record_event(connection, run_id, "incidents", incident_id, "UPSERT", row, body.sim_time_s)
    return row


async def put_road_restriction(connection, run_id, road_id, body):
    """Set a road segment's status (OPEN / LIMITED / BLOCKED) for this run.

    Keyed by (run_id, road_id), so each run has its own copy of road statuses
    and can't collide with other runs.
    """
    run = await writable_run(connection, run_id)
    await check_static(connection, run, "road_segments", road_id)
    await check_observations(connection, run_id,
                             [body.source_observation_id] if body.source_observation_id else [])
    row = await (await connection.execute(
        '''INSERT INTO ops.road_restrictions (run_id,road_id,status,source_observation_id)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (run_id,road_id) DO UPDATE SET status=EXCLUDED.status,
             source_observation_id=EXCLUDED.source_observation_id, updated_at=now()
           WHERE (ops.road_restrictions.status,ops.road_restrictions.source_observation_id)
               IS DISTINCT FROM (EXCLUDED.status,EXCLUDED.source_observation_id)
           RETURNING run_id,road_id,status,source_observation_id,updated_at''',
        (run_id, road_id, body.status, body.source_observation_id),
    )).fetchone()
    if row is None:
        # Unchanged: return the existing row without logging an event.
        row = await (await connection.execute(
            "SELECT run_id,road_id,status,source_observation_id,updated_at FROM ops.road_restrictions WHERE run_id=%s AND road_id=%s",
            (run_id, road_id),
        )).fetchone()
    else:
        await record_event(connection, run_id, "road_restrictions", road_id, "UPSERT", row, body.sim_time_s)
    return row


# =============================================================================
# 7. EVENT LOG
#    ops.operational_events is an append-only history: one entry per change
#    to any ops.* row. It lets you replay or audit a run afterwards.
# =============================================================================

async def record_event(connection, run_id, table, row_id, operation, row, sim_time_s=None):
    """Append one entry to ops.operational_events.

    payload = {"operation": "INSERT"/"UPDATE"/"UPSERT", "row": <the new row>,
               "sim_time_s": <simulation clock, if known>}
    jsonable_encoder turns UUIDs, datetimes, etc. into JSON-safe values first.
    """
    # The v1 schema has reference-validation triggers, but no logging triggers.
    # Append exactly once per changed row, in the caller's transaction.
    await connection.execute(
        "INSERT INTO ops.operational_events (run_id,table_name,row_id,payload) VALUES (%s,%s,%s,%s)",
        (run_id, table, str(row_id), Jsonb(jsonable_encoder({
            "operation": operation, "row": row, "sim_time_s": sim_time_s,
        }))),
    )
