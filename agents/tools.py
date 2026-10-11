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
SIM_URL = setting('SIM_URL', 'http://127.0.0.1:8100').rstrip('/')   # the simulator service: units, routes, dispatch
AGENTS_DATABASE_URL = setting('AGENTS_DATABASE_URL')
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
                        person_groups=None, evidence=None, incident_id=None, expected_updated_at=None, sim_time_s=None,
                        **extra):
        """Create (no incident_id) or replace (incident_id given) one incident. Pass the FULL new state:
        this replaces the incident, it does not merge. Returns the stored incident.

        A replace needs expected_updated_at, the updated_at of the incident as the caller read it. If anyone
        wrote the incident since, nothing is written and ToolError returns its current state, so a writer that
        read a stale copy can't overwrite another writer's change (agents run in parallel).

        No duplicates: a create for something this run already has (an incident citing one of the same
        reports, or on the same building) is merged into that incident instead, and the result says
        "merged_into_existing": true. A per-run lock makes the check-and-merge one step.

        location {longitude, latitude}; person_groups [{group_id, est_count, condition, needs, confidence}];
        evidence {supports: [observation ids], contradicts: [observation ids]}."""
        if status not in INCIDENT_STATUSES:
            raise ToolError(f'status must be one of {INCIDENT_STATUSES}, got {status!r}')
        if incident_id is not None and not _is_uuid(incident_id):
            raise ToolError(f'incident_id {incident_id!r} is not an incident id: use an id from list_incidents or '
                            f'find_nearby_incidents to update, or leave incident_id out to create a new incident')
        if incident_id and not expected_updated_at:
            raise ToolError(f'to replace incident {incident_id}, pass expected_updated_at: its updated_at as you just '
                            'read it with list_incidents or find_nearby_incidents')
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
                    '''SELECT id, status, building_id, uncertainty_m, person_groups, evidence, updated_at,
                              public.ST_X(geom) AS lon, public.ST_Y(geom) AS lat
                       FROM ops.incidents
                       WHERE run_id = %s AND (evidence->'supports' ?| %s::text[] OR (%s::text IS NOT NULL AND building_id = %s))
                       ORDER BY created_at LIMIT 1''',
                    (self.run_id, evidence['supports'], building_id, building_id)).fetchone()
                if existing:
                    new_id, merged = str(existing['id']), True
                    expected_updated_at = existing['updated_at']   # read just now, under the lock
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
                          AND i.updated_at = %s::timestamptz
                          AND (i.geom, i.uncertainty_m, i.status, i.building_id, i.person_groups, i.evidence)
                              IS DISTINCT FROM (EXCLUDED.geom, EXCLUDED.uncertainty_m, EXCLUDED.status,
                                                EXCLUDED.building_id, EXCLUDED.person_groups, EXCLUDED.evidence)
                        RETURNING {INCIDENT_COLUMNS}, (xmax = 0) AS created''',
                    (new_id, self.run_id, lon, lon, lat, uncertainty_m, status, building_id,
                     Jsonb(person_groups) if person_groups is not None else None, Jsonb(evidence),
                     expected_updated_at)).fetchone()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise ToolError(f'building_id {building_id!r} is not on this run\'s map: use an id from find_nearby_buildings') from exc
            except psycopg.errors.InvalidDatetimeFormat as exc:
                raise ToolError(f'expected_updated_at {expected_updated_at!r} is not a time: copy updated_at from the incident') from exc
            if row is None:   # unchanged, changed by someone else since it was read, or the id belongs to another run
                existing = conn.execute(f'''SELECT {INCIDENT_COLUMNS}, updated_at = %s::timestamptz AS as_read
                                            FROM ops.incidents WHERE id = %s AND run_id = %s''',
                                        (expected_updated_at, new_id, self.run_id)).fetchone()
                if existing is None:
                    raise ToolError(f'incident {new_id} belongs to another run')
                existing = dict(existing)
                if not existing.pop('as_read'):
                    raise ToolError(f'incident {new_id} was changed by another writer after you read it; nothing was '
                                    f'written. Its current state is {json.dumps(_json(existing), ensure_ascii=False)}. '
                                    'Combine your change with it and call upsert_incident again with this updated_at.')
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

    # ---- response: units, routes, dispatch (the simulator service) ----------
    # The simulator owns the units: it knows where each one is, plans on the roads the agents believe
    # open (never the truth), and moves a unit once it is dispatched. These tools only ask it.

    def _incident_place(self, incident_id):
        """Where an incident is, as the simulator wants it: its building, else its point."""
        try:
            with _db() as conn:
                row = conn.execute(f'SELECT {INCIDENT_COLUMNS} FROM ops.incidents WHERE run_id = %s AND id = %s',
                                   (self.run_id, incident_id)).fetchone()
        except psycopg.errors.InvalidTextRepresentation as exc:
            raise ToolError('incident_id must be an id from incidents_to_cover or list_incidents') from exc
        if row is None:
            raise ToolError(f'incident {incident_id} is not in this event: use an id from incidents_to_cover')
        if row['building_id'] in self.map.building_geoms:
            return {'building_id': row['building_id']}
        if row['location']:
            return {'lon': row['location']['longitude'], 'lat': row['location']['latitude']}
        raise ToolError(f'incident {incident_id} has no location yet, so nobody can be sent to it')

    def _units(self):
        if _sim('GET', '/clock').get('run_id') != self.run_id:
            raise ToolError('this event is not the one being played in the simulator, so it has no units')
        return _sim('GET', '/units')

    def list_resources(self, status=None, type=None):
        """The responder units right now: type, status (available / en_route / busy), where each is, the
        incident it was last sent to, and how many people it has helped on that job."""
        return [{'unit_id': u['id'], 'name': u['name'], 'type': u['type'], 'status': u['status'],
                 'longitude': u['lon'], 'latitude': u['lat'],
                 'incident_id': (u['destination'] or {}).get('incident_id'), 'helped': u['helped']}
                for u in self._units() if status in (None, u['status']) and type in (None, u['type'])]

    def nearest_units(self, incident_id, type=None, k=3):
        """The k available units (of a type, if given) that can reach an incident soonest, with travel_time_s.
        Units with no way there on the roads believed open are left out."""
        body = {**self._incident_place(incident_id), 'run_id': self.run_id, 'k': k, **({'type': type} if type else {})}
        return _sim('POST', '/tools/nearest-units', body)

    def plan_route(self, unit_id, incident_id):
        """Whether a unit can reach an incident and how long it would take. Sends nobody."""
        r = _sim('POST', '/tools/route', {**self._incident_place(incident_id), 'run_id': self.run_id, 'unit_id': unit_id})
        if not r.get('reachable'):
            return {'reachable': False}
        fallback = r.get('fallback')
        return {'reachable': True, 'travel_time_s': r['travel_time_s'], 'length_m': r['length_m'],
                'has_fallback': bool(fallback), 'fallback_travel_time_s': fallback['travel_time_s'] if fallback else None}

    def dispatch_unit(self, unit_id, incident_id):
        """Send an available unit to an incident. It sets off at once; the decision is recorded in ops.decisions."""
        d = _sim('POST', '/dispatch', {**self._incident_place(incident_id), 'run_id': self.run_id,
                                       'unit_id': unit_id, 'incident_id': incident_id})
        return {'dispatched': True, 'decision_id': d['id'], 'version': d['version'], 'unit_id': unit_id,
                'incident_id': incident_id, 'eta_s': d['eta_s']}

    # ---- proposals: the response agent suggests, an operator approves ------------------
    # A proposal is an ops.decisions row with status 'draft'. The website lists them under each incident with
    # Approve / Reject: Approve dispatches the unit through the simulator (POST /dispatch) and marks the draft
    # 'approved'; Reject marks it 'invalidated'. Drafts use versions from PROPOSAL_VERSION_BASE up, because the
    # simulator numbers the decisions it records when a unit is really sent 1, 2, 3... per incident.

    PROPOSAL_VERSION_BASE = 1000

    def proposals(self, status='draft'):
        """Proposed dispatches (decisions with a proposal version), optionally of one status."""
        return [d for d in _get(f'/runs/{self.run_id}/decisions', limit=1000)
                if d['version'] >= self.PROPOSAL_VERSION_BASE and status in (None, d['status'])]

    def propose_dispatch(self, unit_id, incident_id, unit_type=None, eta_s=None, why=None):
        """Save 'send this unit to this incident' as a draft for an operator to approve. Sends nobody."""
        taken = [d['version'] for d in _get(f'/runs/{self.run_id}/decisions', limit=1000)
                 if d['incident_id'] == incident_id and d['version'] >= self.PROPOSAL_VERSION_BASE]
        body = {'incident_id': incident_id, 'version': max(taken, default=self.PROPOSAL_VERSION_BASE - 1) + 1,
                'assignments': [{'resource_id': unit_id, 'type': unit_type, 'proposed_by': 'response_agent',
                                 **({'why': why} if why else {})}],
                'eta_s': eta_s, 'status': 'draft'}
        return _put(f'/runs/{self.run_id}/decisions/{uuid.uuid4()}', body)

    def close_proposal(self, decision, status, reason=None):
        """Mark a proposal approved / invalidated (keeps everything else as it was)."""
        body = {k: decision.get(k) for k in ('incident_id', 'version', 'assignments', 'eta_s')}
        return _put(f'/runs/{self.run_id}/decisions/{decision["id"]}',
                    {**body, 'status': status, 'invalidated_reason': reason})

    def list_decisions(self, status=None):
        """Dispatch decisions of this event: who was sent where, and which were invalidated and why."""
        return [{'decision_id': d['id'], 'incident_id': d['incident_id'], 'version': d['version'], 'status': d['status'],
                 'unit_ids': [a.get('resource_id') for a in d['assignments'] or []], 'eta_s': d['eta_s'],
                 'invalidated_reason': d['invalidated_reason']}
                for d in _get(f'/runs/{self.run_id}/decisions', limit=1000) if status in (None, d['status'])]

    def list_field_reports(self):
        """What units reported first-hand, oldest first: a road found BLOCKED, or what they did or saw on
        scene (action: assessed / rescued / treated / nothing_found, with counts)."""
        out = []
        for o in self.list_observations():
            if o['channel'] != 'responder_update':
                continue
            try:
                facts = json.loads(o['original_text'])
            except (TypeError, ValueError):
                continue
            out.append({'observation_id': o['id'], 'sim_time_s': o['sim_time_s'], **facts, 'text': o['text_en']})
        return sorted(out, key=lambda r: r['sim_time_s'])

    def dispatch_units(self, assignments):
        """Send several units at once: [{unit_id, incident_id}, ...]. Each is tried on its own, so one that
        cannot go (not available, no way there, used twice in the list) does not stop the others."""
        sent, refused, used = [], [], set()
        for a in assignments or []:
            unit_id, incident_id = (a or {}).get('unit_id') or (a or {}).get('unit'), (a or {}).get('incident_id') or (a or {}).get('incident')
            try:
                if not unit_id or not incident_id:
                    raise ToolError('each assignment needs a unit and an incident')
                if unit_id in used:
                    raise ToolError('this unit is already used earlier in this list: a unit goes to one incident')
                d = self.dispatch_unit(unit_id, incident_id)
                used.add(unit_id)
                sent.append({'unit_id': unit_id, 'incident_id': incident_id, 'eta_s': d['eta_s'], 'decision_id': d['decision_id']})
            except ToolError as exc:
                refused.append({'unit_id': unit_id, 'incident_id': incident_id, 'reason': str(exc)})
        return {'sent': sent, 'refused': refused}

    def incidents_to_cover(self, limit=10):
        """The response picture in one call: every incident that is not resolved, with the people believed
        there by condition, the units already on it, what units reported from the scene, the kinds of unit it
        could still use, and the nearest free units of those kinds; plus how many units of each type are free.
        Most urgent first; only the first `limit` that could use a unit are listed."""
        units = self._units()
        unit_type = {u['id']: u['type'] for u in units}
        reports = {}
        for r in self.list_field_reports():
            if r.get('incident_id') and r.get('action'):
                reports.setdefault(r['incident_id'], []).append(
                    {'unit_type': unit_type.get(r.get('unit_id')),
                     **{k: v for k, v in r.items() if k not in ('observation_id', 'unit_id', 'incident_id', 'building_id', 'text')}})
        incidents = []
        for i in self.list_incidents():
            if i['status'] == 'resolved':
                continue
            people, needs = {}, set()
            for g in i['person_groups'] or []:
                condition = str(g.get('condition') or 'unknown').lower()
                people[condition] = people.get(condition, 0) + int(g.get('est_count') or 0)
                needs.update(g.get('needs') or [])
            on_it = [{'unit_id': u['id'], 'type': u['type'], 'status': u['status']} for u in units
                     if u['status'] != 'available' and (u['destination'] or {}).get('incident_id') == i['id']]
            field = reports.get(i['id'], [])
            located = bool(i['building_id'] or i['location'])
            incidents.append({'incident_id': i['id'], 'status': i['status'], 'people': people, 'needs': sorted(needs),
                              'has_location': located, 'units_on_it': on_it, 'field_reports': field[-3:],
                              'could_use': _could_use(i['status'], people, on_it, field) if located else []})
        free = {t: sum(1 for u in units if u['type'] == t and u['status'] == 'available') for t in ('fire_engine', 'ambulance', 'police')}
        urgency = lambda i: (-i['people'].get('critical', 0), -i['people'].get('trapped', 0))
        open_, rest = [i for i in incidents if i['could_use']], [i for i in incidents if not i['could_use']]
        open_.sort(key=urgency)
        for i in open_[:limit]:   # who could go: asked of the simulator here, so the model does not have to
            i['nearest_free'] = {}
            for t in dict.fromkeys(i['could_use']):
                if free[t]:
                    try:
                        i['nearest_free'][t] = [{'unit_id': n['unit_id'], 'name': n['name'], 'travel_time_s': round(n['travel_time_s'])}
                                                for n in self.nearest_units(i['incident_id'], type=t, k=3)]
                    except ToolError:
                        i['nearest_free'][t] = []
                else:
                    i['nearest_free'][t] = []
        # A first plan by the plain rule: most urgent incident first, the nearest unit nobody else has taken.
        suggested, taken = [], set()
        for i in open_[:limit]:
            for t in i['could_use']:
                unit = next((u for u in i['nearest_free'][t] if u['unit_id'] not in taken), None)
                if unit:
                    taken.add(unit['unit_id'])
                    suggested.append({'unit_id': unit['unit_id'], 'incident_id': i['incident_id'], 'type': t})
        return {'incidents': open_[:limit], 'free_units': free, 'suggested': suggested,
                'not_listed': {'more_that_could_use_a_unit': max(0, len(open_) - limit), 'nothing_needed_now': len(rest)}}

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
                         'expected_updated_at': {'type': 'string', 'description': 'required with incident_id: the '
                                                 'incident\'s updated_at exactly as you read it. If someone changed it '
                                                 'since, you get its current state back to merge with, and retry.'},
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
    'incidents_to_cover': ('The response picture in one call: unresolved incidents that could use a unit, most urgent '
                           'first, with the people believed there, units already on it, field reports, could_use '
                           '(unit types still useful) and nearest_free (nearest available units of those types); '
                           'plus free_units per type.', {}, []),
    'dispatch_units': ('Send several units at once. Each sets off immediately. Returns which were sent (with eta_s) and '
                       'which were refused and why. A unit may appear only once in the list.',
                       {'assignments': {'type': 'array', 'items': {'type': 'object', 'properties': {
                           'unit': {'type': 'string', 'description': 'a unit from your input, e.g. "A1"'},
                           'incident': {'type': 'string', 'description': 'an incident from your input, e.g. "I2"'}},
                           'required': ['unit', 'incident']}}}, ['assignments']),
    'list_resources': ('Responder units right now: type (fire_engine / ambulance / police), status (available / '
                       'en_route / busy), position, the incident each was last sent to.',
                       {'status': {'type': 'string', 'enum': ['available', 'en_route', 'busy']},
                        'type': {'type': 'string', 'enum': ['fire_engine', 'ambulance', 'police']}}, []),
    'nearest_units': ('The k available units of a type that can reach an incident soonest, with travel_time_s. '
                      'Units that cannot get there are left out.',
                      {'incident_id': {'type': 'string'}, 'type': {'type': 'string', 'enum': ['fire_engine', 'ambulance', 'police']},
                       'k': {'type': 'integer'}}, ['incident_id']),
    'plan_route': ('Whether a unit can reach an incident, the travel time, and whether a fallback route exists. Sends nobody.',
                   {'unit_id': {'type': 'string'}, 'incident_id': {'type': 'string'}}, ['unit_id', 'incident_id']),
    'dispatch_unit': ('Send one available unit to one incident. It sets off at once. Returns the decision, or an '
                      'error saying why it cannot go (not available, or no way there).',
                      {'unit_id': {'type': 'string', 'description': 'only a unit_id returned by nearest_units'},
                       'incident_id': {'type': 'string', 'description': 'only an incident_id from incidents_to_cover'}},
                      ['unit_id', 'incident_id']),
    'list_decisions': ('Dispatch decisions so far: unit, incident, status (sent / invalidated) and why one was invalidated.',
                       {'status': {'type': 'string', 'enum': ['sent', 'invalidated']}}, []),
    'list_field_reports': ('What units reported first-hand, oldest first: roads found BLOCKED, and what they did or '
                           'saw on scene (assessed / rescued / treated / nothing_found, with counts).', {}, []),
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


SECOND_ENGINE_FROM = 8   # trapped people from which a second engine is worth sending


def _could_use(status, people, on_it, field_reports):
    """The kinds of unit an incident could still use, from the rescue rules: an engine frees the trapped, an
    ambulance treats the critical, a patrol checks what is unclear. What a unit reported first-hand overrides
    the estimate. Facts for the response agent to decide with; it still chooses who goes and in what order."""
    trapped, critical = people.get('trapped', 0), people.get('critical', 0)
    assessed = False
    for r in field_reports:   # oldest first, so the latest word wins
        if r.get('action') == 'assessed':
            trapped, critical, assessed = r.get('trapped', 0), r.get('critical', 0), True
        elif r.get('action') == 'rescued':
            trapped = r.get('still_trapped', 0)
        elif r.get('action') == 'treated':
            critical = r.get('still_critical', 0)
        elif r.get('action') == 'nothing_found':
            if r.get('unit_type') == 'fire_engine':
                trapped = 0
            elif r.get('unit_type') == 'ambulance':
                critical = 0
            assessed = True
    there = [u['type'] for u in on_it]
    out = []
    if critical and 'ambulance' not in there:
        out.append('ambulance')
    out += ['fire_engine'] * max(0, ((2 if trapped >= SECOND_ENGINE_FROM else 1) if trapped else 0) - there.count('fire_engine'))
    if not trapped and not critical and not assessed and not there and (status == 'disputed' or not people):
        out.append('police')
    return out


def _sim(method, path, body=None):
    """One request to the simulator service. A refusal (unit not free, no way there, wrong run) becomes a
    ToolError carrying the simulator's reason, so the model can read it and choose differently."""
    try:
        r = httpx.request(method, SIM_URL + path, json=body, timeout=60)
    except httpx.HTTPError as exc:
        raise ToolError(f'the simulator is not reachable at {SIM_URL} ({type(exc).__name__}); is ./dev.sh running?') from None
    if r.status_code in (409, 422):
        detail = r.json().get('detail')
        raise ToolError(detail if isinstance(detail, str) else json.dumps(detail))
    return r.raise_for_status().json()


def _put(path, body):
    r = httpx.put(BACKEND_URL + path, json=body, timeout=60)
    if r.status_code in (409, 422):
        raise ToolError(f'backend refused ({r.status_code}): {r.json().get("detail")}')
    return r.raise_for_status().json()


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
