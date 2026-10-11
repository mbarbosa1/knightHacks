'''
Tools the agents use, scoped to one run (event), as the agent .md files describe them:
"Every tool is already scoped to the current event, so you never pass a run_id."

    from agents.tools import RunTools
    tools = RunTools.for_run(run_id)        # loads the run's map version once (a few seconds)
    tools.get_run()
    tools.list_observations(after=None, ids=None)
    tools.find_nearby_buildings(lon, lat, radius_m, building_type=None, floors=None)
    tools.find_nearest_road(lon, lat, street_name=None)

    tools.list_incidents(updated_since=None)
    tools.find_nearby_incidents(lon, lat, radius_m)
    tools.get_last_agent_run(agent)
    tools.upsert_incident(status, location, uncertainty_m, building_id, person_groups, evidence, incident_id=None)
    tools.set_road_restriction(road_id, status, source_observation_id)
    tools.log_agent_run(agent, input_ref, output, duration_ms)

Two kinds of access:
  - Reports and the run come through the backend API (http://127.0.0.1:8000, started by ./dev.sh); the
    map helpers come from simulation/sim/. No database login needed.
  - Incidents, road restrictions and agent runs are read and written in Neon directly as the agents
    role (AGENTS_DATABASE_URL in backend/.env; it can only touch the tables listed in its grants).
    Every write follows the backend's rules (backend/app/repository.py): the run must still be live,
    observation ids must belong to this run, unchanged rows are not rewritten, and each change adds one
    ops.operational_events row {"operation", "row", "sim_time_s"} in the same transaction.

Tools return plain dicts/lists (JSON-ready), so the same functions can be called by code or offered
to the model as tool calls. They never invent ids: every building_id / road_id comes from the map.
A write tool that rejects its input raises ToolError with a message saying what to fix.
'''
import datetime, json, pathlib, sys, uuid
import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from shapely import STRtree
from shapely.geometry import Point

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'simulation'))   # the simulator's map loader and lookups (sim/)
from sim.api_storage import ApiStorage         # noqa: E402
from sim.lookup import MapIndex                # noqa: E402
from sim.roads import to_metres                # noqa: E402
from agents.llm import setting                 # noqa: E402

BACKEND_URL = setting('BACKEND_URL', 'http://127.0.0.1:8000').rstrip('/')
AGENTS_DATABASE_URL = setting('AGENTS_DATABASE_URL')
SIM_URL = setting('SIM_URL', 'http://127.0.0.1:8100').rstrip('/')   # simulator service: Gerard's routing tools
DECISION_COLUMNS = 'id, run_id, incident_id, version, assignments, route_geojson, fallback_route_geojson, eta_s, status, invalidated_reason, approved_by, approved_at'
INCIDENT_STATUSES = ('reported', 'corroborated', 'disputed', 'resolved')
ROAD_STATUSES = ('OPEN', 'LIMITED', 'BLOCKED')
INCIDENT_COLUMNS = '''id, run_id, status, building_id, uncertainty_m, person_groups, evidence, created_at, updated_at,
                      CASE WHEN geom IS NULL THEN NULL ELSE
                        json_build_object('longitude', public.ST_X(geom), 'latitude', public.ST_Y(geom)) END AS location'''


class ToolError(ValueError):
    """A tool rejected its input; the message says what to fix."""

# Words a report uses for a building -> PLATEAU usage values in static.buildings.usage.
BUILDING_TYPES = {'office': {'office'}, 'commercial': {'commercial', 'mixed_commercial'},
                  'apartment': {'apartment'}, 'house': {'house', 'house_with_workshop'},
                  'house_with_shop': {'house_with_shop'}, 'hotel': {'hotel'},
                  'education_welfare': {'education_welfare'}, 'government': {'government'}}


class RunTools:
    def __init__(self, run, static_map):
        self.run = run
        self.run_id = run['id']
        self.map = static_map
        self._roads = MapIndex(static_map)
        self._bldg_ids = list(static_map.building_geoms)
        self._bldg_m = [to_metres(static_map.building_geoms[i]) for i in self._bldg_ids]
        self._bldgs = STRtree(self._bldg_m)

    @classmethod
    def for_run(cls, run_id):
        run = _get(f'/runs/{run_id}')
        return cls(run, ApiStorage(BACKEND_URL).load_static_map(run['map_version_id']))

    # ---- event and reports -------------------------------------------------

    def get_run(self):
        """The event: id, map_version_id, seed, hazard_types, status (running = live), started/ended."""
        return _get(f'/runs/{self.run_id}')

    def ingestion_status(self):
        """{"ready": true, ...} once all of the run's initial reports and units are in Neon.
        Agents must not start before ready is true (docs/agent-data-handoff.md)."""
        return _get(f'/runs/{self.run_id}/ingestion')

    def list_observations(self, after=None, ids=None):
        """Reports in feed order. ids: only these; after: only reports after this observation id."""
        if ids:
            found, missing = [], []
            for i in ids:
                try:
                    found.append(_get(f'/runs/{self.run_id}/observations/{i}'))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in (404, 422):
                        raise
                    missing.append(i)
            if missing:
                raise ToolError(f'these ids are not reports of this event: {missing}. Use only observation_ids '
                                f'from your input or from list_observations() results'
                                + (f'; these were found: {[o["id"] for o in found]}' if found else ''))
            return found
        out = []
        while True:
            page = _get(f'/runs/{self.run_id}/observations', limit=500, **({'after': after} if after else {}))
            out.extend(page)
            if len(page) < 500:
                return out
            after = page[-1]['id']

    # ---- map ----------------------------------------------------------------

    def find_nearby_buildings(self, lon, lat, radius_m, building_type=None, floors=None, k=3):
        """Buildings within radius_m of a point, best first.

        With a description (building_type from BUILDING_TYPES, floors), buildings that fit it come first
        (floors within 1), then the nearest. Returns {"buildings": [...], "fitting_count": n}; a high
        fitting_count means the place is ambiguous and should be confirmed by other reports.
        Each building: building_id, distance_m, usage, floors, structure, era, soil_amp, name_en,
        fits_description (true / false / null when nothing was described)."""
        p = to_metres(Point(lon, lat))
        found = []
        for i in self._bldgs.query(p.buffer(radius_m)):
            d = self._bldg_m[i].distance(p)
            if d > radius_m:
                continue
            props = self.map.buildings[self._bldg_ids[i]]
            fits_type = (None if building_type not in BUILDING_TYPES or props.get('usage') is None
                         else props['usage'] in BUILDING_TYPES[building_type])
            fits_floors = None if floors is None or props.get('floors') is None else abs(props['floors'] - floors) <= 1
            misfits = (fits_type is False) + (fits_floors is False)
            described = fits_type is not None or fits_floors is not None
            found.append((misfits, d, {
                'building_id': self._bldg_ids[i], 'distance_m': round(d, 1), 'usage': props.get('usage'),
                'floors': props.get('floors'), 'structure': props.get('structure'), 'era': props.get('era'),
                'soil_amp': props.get('soil_amp'), 'name_en': props.get('name_en'),
                'fits_description': (misfits == 0) if described else None}))
        found.sort(key=lambda t: (t[0], t[1]))
        return {'buildings': [b for _, _, b in found[:k]],
                'fitting_count': sum(1 for _, _, b in found if b['fits_description'])}

    def find_nearest_road(self, lon, lat, street_name=None, radius_m=100, k=4):
        """Road segments near a point, nearest first; with street_name, segments on that street first
        (searched up to 4x radius_m). Both directions of a two-way street are separate road ids.
        Each: road_id, name_en, name_ja, distance_m, name_match."""
        return self._roads.roads_near(lon, lat, street_name, radius_m=radius_m, k=k)

    # ---- incidents and roads (Neon, agents role) ---------------------------

    def list_incidents(self, updated_since=None):
        """The current incident picture, oldest first; with updated_since (ISO time), only what changed since."""
        with _db() as conn:
            return _json(conn.execute(
                f'''SELECT {INCIDENT_COLUMNS} FROM ops.incidents
                    WHERE run_id = %s AND (%s::timestamptz IS NULL OR updated_at > %s::timestamptz)
                    ORDER BY created_at, id''', (self.run_id, updated_since, updated_since)).fetchall())

    def find_nearby_incidents(self, lon, lat, radius_m):
        """Incidents within radius_m of a point, nearest first, each with distance_m."""
        with _db() as conn:
            return _json(conn.execute(
                f'''SELECT {INCIDENT_COLUMNS},
                           round(public.ST_Distance(geom::public.geography,
                                 public.ST_SetSRID(public.ST_MakePoint(%s, %s), 4326)::public.geography)::numeric, 1) AS distance_m
                    FROM ops.incidents
                    WHERE run_id = %s AND geom IS NOT NULL
                      AND public.ST_DWithin(geom::public.geography,
                          public.ST_SetSRID(public.ST_MakePoint(%s, %s), 4326)::public.geography, %s)
                    ORDER BY distance_m''', (lon, lat, self.run_id, lon, lat, radius_m)).fetchall())

    def list_road_restrictions(self):
        """Roads the agents have marked OPEN / LIMITED / BLOCKED in this run."""
        with _db() as conn:
            return _json(conn.execute(
                'SELECT road_id, status, source_observation_id, updated_at FROM ops.road_restrictions '
                'WHERE run_id = %s ORDER BY road_id', (self.run_id,)).fetchall())

    def upsert_incident(self, status, location=None, uncertainty_m=None, building_id=None,
                        person_groups=None, evidence=None, incident_id=None, sim_time_s=None, **extra):
        """Create (no incident_id) or replace (incident_id given) one incident. Pass the FULL new state:
        this replaces the incident, it does not merge. Returns the stored incident.

        No duplicates: a create for something this run already has (an incident citing one of the same
        reports, or on the same building) is merged into that incident instead, and the result says
        "merged_into_existing": true. Agents running in parallel (detect ‖ medical) both try to create the
        incident for a new report; a per-run lock makes the check-and-create one step.

        location {longitude, latitude}; person_groups [{group_id, est_count, condition, needs, confidence}];
        evidence {supports: [observation ids], contradicts: [observation ids]}."""
        if status not in INCIDENT_STATUSES:
            raise ToolError(f'status must be one of {INCIDENT_STATUSES}, got {status!r}')
        if incident_id is not None and not _is_uuid(incident_id):
            raise ToolError(f'incident_id {incident_id!r} is not an incident id: use an id from list_incidents or '
                            f'find_nearby_incidents to update, or leave incident_id out to create a new incident')
        evidence = {'supports': list((evidence or {}).get('supports') or []),
                    'contradicts': list((evidence or {}).get('contradicts') or [])}
        group_ids = [g.get('group_id') for g in person_groups or []]
        if None in group_ids or len(group_ids) != len(set(group_ids)):
            raise ToolError('every person group needs its own group_id (reuse the old one when refining a group)')
        lon, lat = (location or {}).get('longitude'), (location or {}).get('latitude')
        if (lon is None) != (lat is None):
            raise ToolError('location needs both longitude and latitude, or neither')
        new_id, merged = incident_id or str(uuid.uuid4()), False
        with _db() as conn:
            self._require_live(conn)
            self._require_observations(conn, evidence['supports'] + evidence['contradicts'])
            if incident_id is None:
                conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('incidents:' + self.run_id,))
                existing = conn.execute(
                    '''SELECT id, status, building_id, uncertainty_m, person_groups, evidence,
                              public.ST_X(geom) AS lon, public.ST_Y(geom) AS lat
                       FROM ops.incidents
                       WHERE run_id = %s AND (evidence->'supports' ?| %s::text[] OR (%s::text IS NOT NULL AND building_id = %s))
                       ORDER BY created_at LIMIT 1''',
                    (self.run_id, evidence['supports'], building_id, building_id)).fetchone()
                if existing:
                    new_id, merged = str(existing['id']), True
                    building_id = building_id or existing['building_id']
                    if lon is None and existing['lon'] is not None:
                        lon, lat = existing['lon'], existing['lat']
                    uncertainty_m = uncertainty_m if uncertainty_m is not None else existing['uncertainty_m']
                    person_groups = _merge_groups(existing['person_groups'], person_groups)
                    old = existing['evidence'] or {}
                    evidence = {k: list(dict.fromkeys((old.get(k) or []) + evidence[k])) for k in ('supports', 'contradicts')}
            try:
                row = conn.execute(
                    f'''INSERT INTO ops.incidents AS i (id, run_id, geom, uncertainty_m, status, building_id, person_groups, evidence)
                        VALUES (%s, %s, CASE WHEN %s::double precision IS NULL THEN NULL
                                ELSE public.ST_SetSRID(public.ST_MakePoint(%s, %s), 4326) END, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET geom = EXCLUDED.geom, uncertainty_m = EXCLUDED.uncertainty_m,
                          status = EXCLUDED.status, building_id = EXCLUDED.building_id,
                          person_groups = EXCLUDED.person_groups, evidence = EXCLUDED.evidence, updated_at = now()
                        WHERE i.run_id = EXCLUDED.run_id
                          AND (i.geom, i.uncertainty_m, i.status, i.building_id, i.person_groups, i.evidence)
                              IS DISTINCT FROM (EXCLUDED.geom, EXCLUDED.uncertainty_m, EXCLUDED.status,
                                                EXCLUDED.building_id, EXCLUDED.person_groups, EXCLUDED.evidence)
                        RETURNING {INCIDENT_COLUMNS}, (xmax = 0) AS created''',
                    (new_id, self.run_id, lon, lon, lat, uncertainty_m, status, building_id,
                     Jsonb(person_groups) if person_groups is not None else None, Jsonb(evidence))).fetchone()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise ToolError(f'building_id {building_id!r} is not on this run\'s map: use an id from find_nearby_buildings') from exc
            if row is None:   # unchanged, or the id belongs to another run
                existing = conn.execute(f'SELECT {INCIDENT_COLUMNS} FROM ops.incidents WHERE id = %s AND run_id = %s',
                                        (new_id, self.run_id)).fetchone()
                if existing is None:
                    raise ToolError(f'incident {new_id} belongs to another run')
                return {**_json(existing), **({'merged_into_existing': True} if merged else {})}
            row = dict(row)
            _event(conn, self.run_id, 'incidents', new_id, 'INSERT' if row.pop('created') else 'UPDATE', row, sim_time_s)
            return {**_json(row), **({'merged_into_existing': True} if merged else {}),
                    **({'ignored_fields': sorted(extra), 'note': 'incidents do not store these fields; '
                        'put them (e.g. prediction, priority) in your final answer'} if extra else {})}

    def set_road_restriction(self, road_id, status, source_observation_id=None, sim_time_s=None):
        """Mark one road segment OPEN / LIMITED / BLOCKED (both directions of a street are separate ids).
        A change here is what triggers rerouting."""
        if status not in ROAD_STATUSES:
            raise ToolError(f'status must be one of {ROAD_STATUSES}, got {status!r}')
        if road_id not in self.map.roads:
            raise ToolError(f'road_id {road_id!r} is not on this run\'s map: use an id from find_nearest_road')
        with _db() as conn:
            self._require_live(conn)
            self._require_observations(conn, [source_observation_id] if source_observation_id else [])
            row = conn.execute(
                '''INSERT INTO ops.road_restrictions AS r (run_id, road_id, status, source_observation_id)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (run_id, road_id) DO UPDATE SET status = EXCLUDED.status,
                     source_observation_id = EXCLUDED.source_observation_id, updated_at = now()
                   WHERE (r.status, r.source_observation_id) IS DISTINCT FROM (EXCLUDED.status, EXCLUDED.source_observation_id)
                   RETURNING run_id, road_id, status, source_observation_id, updated_at''',
                (self.run_id, road_id, status, source_observation_id)).fetchone()
            if row is None:   # unchanged
                return _json(conn.execute('SELECT run_id, road_id, status, source_observation_id, updated_at '
                                          'FROM ops.road_restrictions WHERE run_id = %s AND road_id = %s',
                                          (self.run_id, road_id)).fetchone())
            _event(conn, self.run_id, 'road_restrictions', road_id, 'UPSERT', row, sim_time_s)
            return _json(row)

    # ---- response side: units, routes, decisions -----------------------------
    # Routing runs on the simulator service (Gerard's /tools/*, sim/units.py) on the roads the agents BELIEVE
    # are open (ops.road_restrictions), never the truth. Agents only draft decisions: a human approves, and
    # only that approval dispatches a unit (POST /dispatch is not an agent tool).

    def list_resources(self, status=None, type=None):
        """Response units (ambulance / fire_engine / police): id, type, name, status, location, home facility."""
        units = _get(f'/runs/{self.run_id}/resources', limit=1000)
        return [u for u in units if (status is None or u.get('status') == status) and (type is None or u.get('type') == type)]

    def list_units(self):
        """Where every unit is right now in the live run, with its route and decision (for the map)."""
        return _sim('GET', '/units')

    def _place(self, place, prefix=''):
        """{incident_id} | {building_id} | {facility_id} | {unit_id} | {longitude, latitude} -> the simulator's fields."""
        place = place or {}
        if place.get('incident_id'):
            inc = next((i for i in self.list_incidents() if i['id'] == place['incident_id']), None)
            if inc is None:
                raise ToolError(f'incident {place["incident_id"]} is not in this event: use an id from list_incidents')
            if inc.get('building_id') and not prefix:      # a destination: the simulator knows the building
                return {'building_id': inc['building_id']}
            if not inc.get('location'):
                raise ToolError(f'incident {inc["id"]} has no location yet')
            return {f'{prefix}lon': inc['location']['longitude'], f'{prefix}lat': inc['location']['latitude']}
        if place.get('facility_id'):
            f = self.map.facilities.get(place['facility_id'])
            if f is None:
                raise ToolError(f'facility {place["facility_id"]} is not on this map')
            return {f'{prefix}lon': f['lon'], f'{prefix}lat': f['lat']}
        if place.get('building_id') and not prefix:
            if place['building_id'] not in self.map.buildings:
                raise ToolError(f'building_id {place["building_id"]!r} is not on this map')
            return {'building_id': place['building_id']}
        if place.get('unit_id') and prefix:
            return {'unit_id': place['unit_id']}
        if place.get('longitude') is not None and place.get('latitude') is not None:
            return {f'{prefix}lon': place['longitude'], f'{prefix}lat': place['latitude']}
        raise ToolError('give a place as {incident_id}, {building_id}, {facility_id}, {unit_id} (start only) '
                        'or {longitude, latitude}')

    def nearest_units(self, incident_id=None, building_id=None, longitude=None, latitude=None, type=None, k=3):
        """Free units that can reach a place soonest on the roads believed open, fastest first."""
        place = self._place({'incident_id': incident_id, 'building_id': building_id,
                             'longitude': longitude, 'latitude': latitude})
        return _sim('POST', '/tools/nearest-units', {**place, 'type': type, 'k': k, 'run_id': self.run_id})

    def plan_route(self, origin, destination):
        """How a unit (or a point) would get somewhere: reachable, travel_time_s, length_m, roads, GeoJSON.
        Sends nobody."""
        start = self._place(origin, prefix='from_')
        return _sim('POST', '/tools/route', {**start, **self._place(destination), 'run_id': self.run_id})

    def facility_options(self, longitude, latitude, type, k=3):
        """Facilities of a type (hospital / shelter / fire_station / police) ranked by drive time from a point.
        capacity null means unknown, not zero."""
        options = []
        for fid, f in self.map.facilities.items():
            if f.get('type') != type:
                continue
            r = self.plan_route({'longitude': longitude, 'latitude': latitude}, {'facility_id': fid})
            options.append({'facility_id': fid, 'name_en': f.get('name_en'), 'capacity': f.get('capacity'),
                            'reachable': r.get('reachable', False), 'travel_time_s': r.get('travel_time_s')})
        if not options:
            raise ToolError(f'no facility of type {type!r}; types on this map: '
                            f'{sorted({f.get("type") for f in self.map.facilities.values()})}')
        return sorted(options, key=lambda o: (not o['reachable'], o['travel_time_s'] or 1e12))[:k]

    def assign_units(self, incident_ids, type):
        """One free unit of a type per incident, each incident (in the order given) taking the fastest unit
        not already taken. Incidents no free unit can reach are listed in 'unassigned'."""
        taken, out, unassigned = set(), [], []
        for iid in incident_ids:
            options = [u for u in self.nearest_units(incident_id=iid, type=type, k=len(taken) + 3)
                       if u.get('unit_id', u.get('id')) not in taken]
            if not options:
                unassigned.append(iid)
                continue
            unit = options[0]
            taken.add(unit.get('unit_id', unit.get('id')))
            out.append({'incident_id': iid, **unit})
        return {'assignments': out, 'unassigned': unassigned}

    def list_decisions(self, status=None):
        """Response plans in this event (draft / approved / invalidated / sent), latest version first."""
        with _db() as conn:
            return _json(conn.execute(
                f'SELECT {DECISION_COLUMNS} FROM ops.decisions WHERE run_id = %s AND (%s::text IS NULL OR status = %s) '
                'ORDER BY incident_id, version DESC', (self.run_id, status, status)).fetchall())

    def upsert_decision(self, incident_id, assignments, route=None, fallback_route=None, eta_s=None, sim_time_s=None):
        """Save a new DRAFT version of the response plan for one incident. Never approves or sends it:
        an operator approves in the UI. assignments: [{unit_id, role?}]."""
        if not _is_uuid(incident_id):
            raise ToolError(f'incident_id {incident_id!r} is not an incident id: use an id from list_incidents')
        if not isinstance(assignments, list) or not all(isinstance(a, dict) and a.get('unit_id') for a in assignments):
            raise ToolError('assignments must be a list of {unit_id, ...} with unit ids from nearest_units / list_resources')
        known = {u['id'] for u in self.list_resources()}
        unknown = sorted({a['unit_id'] for a in assignments} - known)
        if unknown:
            raise ToolError(f'unknown unit ids {unknown}: use ids from nearest_units or list_resources')
        with _db() as conn:
            self._require_live(conn)
            if not conn.execute('SELECT 1 FROM ops.incidents WHERE id = %s AND run_id = %s', (incident_id, self.run_id)).fetchone():
                raise ToolError(f'incident {incident_id} is not in this event: use an id from list_incidents')
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('decisions:' + str(incident_id),))
            version = conn.execute('SELECT coalesce(max(version), 0) + 1 AS v FROM ops.decisions WHERE incident_id = %s',
                                   (incident_id,)).fetchone()['v']
            row = dict(conn.execute(
                f'''INSERT INTO ops.decisions (run_id, incident_id, version, assignments, route_geojson,
                                               fallback_route_geojson, eta_s, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft') RETURNING {DECISION_COLUMNS}''',
                (self.run_id, incident_id, version, Jsonb(assignments), Jsonb(route) if route else None,
                 Jsonb(fallback_route) if fallback_route else None, eta_s)).fetchone())
            _event(conn, self.run_id, 'decisions', row['id'], 'INSERT', row, sim_time_s)
            return _json(row)

    # ---- agent audit log (ops.agent_runs) ----------------------------------

    def log_agent_run(self, agent, input_ref, output, duration_ms=None, run_key=None):
        """Record one invocation of an agent and its output. Call it every time, even when nothing changed.
        run_key (optional) makes retries safe: logging the same key again replaces that entry."""
        row_id = str(uuid.uuid5(uuid.UUID(self.run_id), f'{agent}:{run_key}')) if run_key else str(uuid.uuid4())
        with _db() as conn:
            row = conn.execute(
                '''INSERT INTO ops.agent_runs AS a (id, run_id, agent, input_ref, output, duration_ms)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET input_ref = EXCLUDED.input_ref, output = EXCLUDED.output,
                     duration_ms = EXCLUDED.duration_ms
                   WHERE a.run_id = EXCLUDED.run_id AND a.output IS DISTINCT FROM EXCLUDED.output
                   RETURNING id, run_id, agent, input_ref, output, duration_ms, created_at, (xmax = 0) AS created''',
                (row_id, self.run_id, agent, input_ref, Jsonb(output), duration_ms)).fetchone()
            if row is not None:
                row = dict(row)
                _event(conn, self.run_id, 'agent_runs', row_id, 'INSERT' if row.pop('created') else 'UPDATE', row)
            return {'id': row_id}

    def get_last_agent_run(self, agent):
        """This agent's most recent entry in this run (when it last ran and what it concluded), or None."""
        with _db() as conn:
            row = conn.execute('SELECT id, agent, input_ref, output, duration_ms, created_at FROM ops.agent_runs '
                               'WHERE run_id = %s AND agent = %s ORDER BY created_at DESC LIMIT 1',
                               (self.run_id, agent)).fetchone()
            return _json(row) if row else None

    def agent_outputs(self, agent):
        """This agent's successful outputs in this run, oldest first (e.g. in_agent records for a later agent)."""
        with _db() as conn:
            return [r['output'] for r in conn.execute(
                "SELECT output FROM ops.agent_runs WHERE run_id = %s AND agent = %s AND output->>'error' IS NULL "
                'ORDER BY created_at', (self.run_id, agent)).fetchall()]

    def assessed_observations(self):
        """Observation ids in a completed assessment batch (the orchestrator's queue bookkeeping)."""
        with _db() as conn:
            return {r['o'] for r in conn.execute(
                """SELECT jsonb_array_elements_text(output->'observation_ids') AS o FROM ops.agent_runs
                   WHERE run_id = %s AND agent = 'orchestrator' AND output->>'stage' = 'assessment'
                     AND coalesce((output->>'complete')::boolean, true)""", (self.run_id,)).fetchall()}

    def processed_inputs(self, agent):
        """input_ref of every successful entry of this agent (output.error is null): work already done."""
        with _db() as conn:
            return {r['input_ref'] for r in conn.execute(
                "SELECT input_ref FROM ops.agent_runs WHERE run_id = %s AND agent = %s AND output->>'error' IS NULL",
                (self.run_id, agent)).fetchall()}

    # ---- checks shared by the write tools ----------------------------------

    def _require_live(self, conn):
        status = conn.execute('SELECT status FROM ops.simulation_runs WHERE id = %s', (self.run_id,)).fetchone()
        if status is None:
            raise ToolError('run not found')
        if status['status'] in ('completed', 'ended'):
            raise ToolError('the run has ended: read-only mode, only log_agent_run is allowed')

    def _require_observations(self, conn, ids):
        if not ids:
            return
        try:
            found = {str(r['id']) for r in conn.execute(
                'SELECT id FROM ops.observations WHERE run_id = %s AND id = ANY(%s::uuid[])', (self.run_id, list(ids))).fetchall()}
        except psycopg.errors.InvalidTextRepresentation as exc:
            raise ToolError('observation ids must be UUIDs taken from list_observations') from exc
        missing = sorted(set(map(str, ids)) - found)
        if missing:
            raise ToolError(f'these observation ids are not reports of this run: {missing}')


    # ---- offering the tools to the model ------------------------------------

    def toolbox(self, agent, names, done=None):
        """(specs, functions) for llm.run_agent: the named tools described for the model, and callables.
        get_last_agent_run is bound to this agent. log_agent_run(input_ref) only records, in `done`, the last
        report the agent processed: the agent then gives its final answer as plain text and the orchestrator
        saves both to ops.agent_runs. (Passing the whole answer inside a tool call made the model server crash:
        "json parser could not parse region as JSON".) An `output` argument, if the model still sends one, is kept."""
        done = {} if done is None else done

        def mark_done(input_ref, output=None, **_):
            done.update(input_ref=input_ref, **({'output': output} if output is not None else {}))
            return {'ok': True, 'next': 'Now reply with your final JSON answer as plain text, not in a tool call.'}

        bound = {'get_last_agent_run': lambda: self.get_last_agent_run(agent), 'log_agent_run': mark_done}
        functions = {n: bound.get(n) or getattr(self, n) for n in names}
        specs = [{'type': 'function', 'function': {'name': n, 'description': TOOL_SPECS[n][0],
                                                   'parameters': {'type': 'object', 'properties': TOOL_SPECS[n][1],
                                                                  'required': TOOL_SPECS[n][2]}}} for n in names]
        return specs, functions


# Tool descriptions the model sees: name -> (what it does, parameters as JSON schema, required).
_POINT = {'lon': {'type': 'number', 'description': 'longitude'}, 'lat': {'type': 'number', 'description': 'latitude'}}
_UUIDS = {'type': 'array', 'items': {'type': 'string'}}
_PLACE = {'type': 'object', 'properties': {k: {'type': t} for k, t in (
    ('incident_id', 'string'), ('building_id', 'string'), ('facility_id', 'string'), ('unit_id', 'string'),
    ('longitude', 'number'), ('latitude', 'number'))}}
TOOL_SPECS = {
    'get_run': ('The current event: status (running = live), hazard_types, map version.', {}, []),
    'list_observations': ('Reports of this event. ids: only these observation ids; after: only reports after this id.',
                          {'ids': _UUIDS, 'after': {'type': 'string'}}, []),
    'list_incidents': ('Current incidents. updated_since (ISO time): only incidents changed since then.',
                       {'updated_since': {'type': 'string'}}, []),
    'list_road_restrictions': ('Roads currently marked OPEN / LIMITED / BLOCKED in this event.', {}, []),
    'get_last_agent_run': ('When you last ran in this event and what you concluded (null if never).', {}, []),
    'find_nearby_incidents': ('Existing incidents within radius_m of a point, nearest first, with distance_m.',
                              {**_POINT, 'radius_m': {'type': 'number'}}, ['lon', 'lat', 'radius_m']),
    'find_nearby_buildings': ('Buildings within radius_m of a point with usage, floors, structure, era, soil_amp. '
                              'Optional building_type and floors put buildings that fit the description first.',
                              {**_POINT, 'radius_m': {'type': 'number'}, 'building_type': {'type': 'string'},
                               'floors': {'type': 'integer'}}, ['lon', 'lat', 'radius_m']),
    'find_nearest_road': ('Road segments near a point (optionally on a named street), nearest first, with road_id.',
                          {**_POINT, 'street_name': {'type': 'string'}}, ['lon', 'lat']),
    'upsert_incident': ('Create (omit incident_id) or replace (give incident_id) an incident. Pass the FULL new '
                        'state; it replaces, it does not merge. Returns the stored incident with its id.',
                        {'incident_id': {'type': 'string'},
                         'status': {'type': 'string', 'enum': list(INCIDENT_STATUSES)},
                         'location': {'type': 'object', 'properties': {'longitude': {'type': 'number'},
                                                                       'latitude': {'type': 'number'}}},
                         'uncertainty_m': {'type': 'number'},
                         'building_id': {'type': 'string', 'description': 'only an id returned by find_nearby_buildings'},
                         'person_groups': {'type': 'array', 'items': {'type': 'object', 'properties': {
                             'group_id': {'type': 'string'}, 'est_count': {'type': 'integer'},
                             'condition': {'type': 'string'}, 'needs': {'type': 'array', 'items': {'type': 'string'}},
                             'confidence': {'type': 'number'}}}},
                         'evidence': {'type': 'object', 'properties': {'supports': _UUIDS, 'contradicts': _UUIDS}}},
                        ['status']),
    'set_road_restriction': ('Mark a road segment OPEN / LIMITED / BLOCKED, citing the report it comes from.',
                             {'road_id': {'type': 'string', 'description': 'only an id returned by find_nearest_road'},
                              'status': {'type': 'string', 'enum': list(ROAD_STATUSES)},
                              'source_observation_id': {'type': 'string'}}, ['road_id', 'status']),
    'list_resources': ('Response units: id, type (ambulance / fire_engine / police), status, location, home facility.',
                       {'status': {'type': 'string', 'enum': ['available', 'en_route', 'busy', 'out_of_service']},
                        'type': {'type': 'string', 'enum': ['ambulance', 'fire_engine', 'police']}}, []),
    'list_units': ('Where every unit is right now, with its route and the decision it follows.', {}, []),
    'nearest_units': ('Free units that can reach a place soonest on roads believed open, fastest first. '
                      'Give incident_id, or building_id, or longitude+latitude.',
                      {'incident_id': {'type': 'string'}, 'building_id': {'type': 'string'},
                       'longitude': {'type': 'number'}, 'latitude': {'type': 'number'},
                       'type': {'type': 'string', 'enum': ['ambulance', 'fire_engine', 'police']},
                       'k': {'type': 'integer'}}, []),
    'plan_route': ('How a unit or a point would get somewhere (reachable, travel_time_s, length_m, roads). Sends nobody. '
                   'origin: {unit_id} or {longitude, latitude} or {incident_id} or {facility_id}; '
                   'destination: {incident_id} or {building_id} or {facility_id} or {longitude, latitude}.',
                   {'origin': _PLACE, 'destination': _PLACE}, ['origin', 'destination']),
    'facility_options': ('Facilities of a type ranked by drive time from a point (capacity null = unknown).',
                         {'longitude': {'type': 'number'}, 'latitude': {'type': 'number'},
                          'type': {'type': 'string', 'enum': ['hospital', 'shelter', 'fire_station', 'police']},
                          'k': {'type': 'integer'}}, ['longitude', 'latitude', 'type']),
    'assign_units': ('One free unit of a type per incident (in the order given), each taking the fastest unit not '
                     'already taken; unreachable incidents are listed as unassigned.',
                     {'incident_ids': _UUIDS, 'type': {'type': 'string', 'enum': ['ambulance', 'fire_engine', 'police']}},
                     ['incident_ids', 'type']),
    'list_decisions': ('Response plans in this event, latest version first.',
                       {'status': {'type': 'string', 'enum': ['draft', 'approved', 'invalidated', 'sent']}}, []),
    'upsert_decision': ('Save a new DRAFT plan for one incident (a human approves it; this never sends anyone). '
                        'assignments: [{unit_id, role}] with unit ids from nearest_units.',
                        {'incident_id': {'type': 'string'},
                         'assignments': {'type': 'array', 'items': {'type': 'object', 'properties': {
                             'unit_id': {'type': 'string'}, 'role': {'type': 'string'}}}},
                         'route': {'type': 'object'}, 'fallback_route': {'type': 'object'}, 'eta_s': {'type': 'number'}},
                        ['incident_id', 'assignments']),
    'log_agent_run': ('Call this last, every time, with only the id of the last report you processed. Then reply '
                      'with your final JSON answer as plain text (not inside a tool call).',
                      {'input_ref': {'type': 'string', 'description': 'last observation id you processed'}},
                      ['input_ref']),
}


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def _merge_groups(old, new):
    """Person groups of two writers for one incident: same group_id -> the newer one, others kept."""
    if new is None:
        return old
    groups = {g.get('group_id'): g for g in old or []}
    groups.update({g['group_id']: g for g in new})
    return list(groups.values())


def _sim(method, path, body=None):
    """Call the simulator service's agent tools. Its 409/422 replies (no live run, unit not free, unknown
    building, no way there) come back as ToolError with its reason, so the model can adjust."""
    try:
        r = httpx.request(method, SIM_URL + path, json={k: v for k, v in (body or {}).items() if v is not None}
                          if body is not None else None, timeout=60)
    except httpx.HTTPError as exc:
        raise ToolError(f'simulator service not reachable at {SIM_URL} ({exc}); is ./dev.sh running?') from None
    if r.status_code in (404, 409, 422):
        detail = r.json().get('detail') if r.headers.get('content-type', '').startswith('application/json') else r.text
        raise ToolError(f'simulator says ({r.status_code}): {detail}')
    r.raise_for_status()
    return r.json()


def _get(path, **params):
    return httpx.get(BACKEND_URL + path, params=params, timeout=60).raise_for_status().json()


def _db():
    """A Neon connection as the agents role; the with-block commits on success and rolls back on error."""
    if not AGENTS_DATABASE_URL:
        raise ToolError('AGENTS_DATABASE_URL is not set in backend/.env (ask Camila for the agents login)')
    return psycopg.connect(AGENTS_DATABASE_URL, row_factory=dict_row)


def _event(conn, run_id, table, row_id, operation, row, sim_time_s=None):
    """One ops.operational_events row, same shape as the backend's record_event."""
    conn.execute('INSERT INTO ops.operational_events (run_id, table_name, row_id, payload) VALUES (%s, %s, %s, %s)',
                 (run_id, table, str(row_id), Jsonb(_json({'operation': operation, 'row': row, 'sim_time_s': sim_time_s}))))


def _json(value):
    """UUIDs, datetimes and Decimals -> JSON-safe values, so tool results can go straight to the model."""
    return json.loads(json.dumps(value, default=lambda v: v.isoformat() if isinstance(v, (datetime.date, datetime.datetime)) else
                                 float(v) if v.__class__.__name__ == 'Decimal' else str(v)))
