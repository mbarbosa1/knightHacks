'''
How the simulator plugs into the FastAPI backend (and through it, Neon)

sim/api_storage.py  The simulator never talks to Neon directly. It sends HTTP requests to the
backend (backend/app/main.py), exactly like the frontend or the agents would. The backend then
validates the body (models.py), runs the SQL (repository.py) and logs every change to
ops.operational_events.

The big picture of one run (sim/run.py start_run):

    simulator                         FastAPI backend                    Neon
    ---------                         ---------------                    ----
    load_static_map()   -- GET  /map/versions/{v}/buildings|roads|facilities -->  static.*
    save_run()          -- POST /runs  -------------------------------------->  ops.simulation_runs (status 'created')
    (compute damage, roads, people = the hidden truth; stays in the simulator)
    save_truth_batch()  -- nothing sent; written to local files only
    save_observation()  -- POST /runs/{id}/observations  ------------------->  ops.observations
                                                     (first one flips the run to 'running')
    end_run()           -- POST /runs/{id}/end  ----------------------------->  status 'completed', ended_at

Why it is built this way:
  - Same method names as LocalStorage (sim/storage.py). run.py only calls these methods, so
    swapping `LocalStorage()` for `ApiStorage(url)` changes where data goes without touching any
    simulation code. That is the "storage interface" from docs/simulator-runtime.md.
  - Only what agents are allowed to see goes to Neon: the run row and the observations (the noisy
    reports). The truth (building damage, road status, people, truth_links = the answer key) never
    goes to Neon, or agents could read it and the reveal-truth scorecard would be meaningless.
    Until the Databricks writer exists, truth is kept on disk under simulation/runs/<run_id>/.
  - Going through the API instead of raw SQL reuses all the rules already in the backend:
    input validation, map/run id checks, retry-safe inserts, run status changes, the event log.

Sections in this file:
  1. Request body conversion  (simulator observation dict -> API JSON)
  2. HTTP helper              (_call: send one request, raise ApiError on 4xx/5xx)
  3. Reading the map          (load_static_map)
  4. Writing a run            (save_run, save_truth_batch, save_observation, end_run)
'''
import json, urllib.error, urllib.request   # urllib is Python's built-in HTTP client: no extra package needed
from shapely.geometry import shape          # turns GeoJSON geometry dicts into shapely shapes (the simulator does geometry math on them)
from .storage import LocalStorage, StaticMap


# =============================================================================
# 1. REQUEST BODY CONVERSION
#    reports.py builds each observation shaped like an ops.observations row.
#    The API expects the ObservationCreate shape (backend/app/models.py) instead,
#    which differs in a few places, so it is converted here.
# =============================================================================

# Fields that are identical in both shapes and are copied over unchanged.
OBSERVATION_FIELDS = ('id', 'channel', 'format', 'original_text', 'lang', 'text_en', 'media_url',
                      'claimed_location', 'location_precision', 'source_reliability', 'sim_time_s')


class ApiError(RuntimeError):
    """The backend answered with an error (e.g. 404 run not found, 409 run has ended, 422 bad body)."""
    pass


def observation_body(obs):
    """Simulator observation dict -> JSON body for POST /runs/{id}/observations.

    Differences between the two shapes:
      - claimed_geom (GeoJSON point {"type": "Point", "coordinates": [lon, lat]}) becomes
        location {"longitude", "latitude"}, which is what ObservationCreate.location expects.
        The backend turns it back into a PostGIS point (ST_MakePoint in repository.save_observation).
      - run_id is left out: it goes in the URL instead.
      - received_at is left out: the database sets it to now() at the moment the report is stored,
        i.e. when the report is "released" to the agents.
    models.py forbids unknown fields (extra="forbid"), so sending anything extra would be a 422.
    """
    body = {k: obs.get(k) for k in OBSERVATION_FIELDS}
    geom = obs.get('claimed_geom')
    # Vague reports ("somewhere near the station") have no point at all -> location null.
    body['location'] = {'longitude': geom['coordinates'][0], 'latitude': geom['coordinates'][1]} if geom else None
    return body


class ApiStorage:
    def __init__(self, base_url='http://127.0.0.1:8000', truth_storage=None, timeout=120):
        # base_url: where uvicorn is serving the backend (no trailing slash, so paths join cleanly).
        # timeout: seconds to wait per request; the bulk buildings layer is ~6 MB, so it is generous.
        self.base_url, self.timeout = base_url.rstrip('/'), timeout
        # Where everything that must NOT go to Neon is written (truth rows), plus a local copy of the
        # run and its observations so scripts/check_sim_run.py can compare Neon against them.
        self.truth_storage = truth_storage or LocalStorage()

    # =========================================================================
    # 2. HTTP HELPER
    #    Every request to the backend goes through _call.
    # =========================================================================

    def _call(self, method, path, body=None):
        """Send one JSON request to the backend and return the decoded JSON response.

        Example: self._call('POST', '/runs', {...}) -> the new ops.simulation_runs row as a dict.
        """
        req = urllib.request.Request(self.base_url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # urllib raises on any 4xx/5xx. Re-raise with the backend's {"detail": ...} message so the
            # error says why (e.g. "409: Run has ended") instead of just the status code.
            raise ApiError(f'{method} {path} -> {e.code}: {e.read().decode(errors="replace")}') from None

    # =========================================================================
    # 3. READING THE MAP
    #    The simulator needs every building, road and facility to compute damage.
    #    Instead of local GeoJSON files (LocalStorage), it downloads them from
    #    Neon's static.* tables through the backend's bulk map endpoints.
    # =========================================================================

    def load_static_map(self, version='shinjuku-v1'):
        """The map from Neon static.* via the API, in the same StaticMap shape LocalStorage returns.

        No road graph yet (graph=None): start_run does not route, and building the NetworkX graph
        from road_segments is a later step.
        """
        base = f'/map/versions/{version}'
        b = self._call('GET', base + '/buildings')['features']   # repository.bulk_map -> GeoJSON FeatureCollection
        r = self._call('GET', base + '/roads')['features']       # same, for static.road_segments
        f = self._call('GET', base + '/facilities')              # repository.list_facilities -> plain list of rows

        roads = {}
        for x in r:
            # The API sends node ids as strings because JavaScript loses precision on big integers;
            # the simulator (like OSMnx/NetworkX) uses ints, so convert them back.
            p = x['properties']
            roads[p['id']] = {**p, 'from_node': int(p['from_node']), 'to_node': int(p['to_node'])}

        # StaticMap (sim/storage.py) keeps properties and geometry in separate dicts, all keyed by id
        # (building id bldg_<uuid>, road id u_v_key, facility id) -- the ids shared across map, graph and DB.
        return StaticMap(
            version,
            {x['properties']['id']: x['properties'] for x in b},           # buildings: id -> columns
            {x['properties']['id']: shape(x['geometry']) for x in b},      # buildings: id -> Polygon
            {x['id']: {**{k: v for k, v in x.items() if k != 'geometry'},  # facilities: id -> columns + lon/lat
                       'lon': x['geometry']['coordinates'][0], 'lat': x['geometry']['coordinates'][1]} for x in f},
            roads,                                                         # roads: id -> columns
            {x['properties']['id']: shape(x['geometry']) for x in r})      # roads: id -> LineString

    # =========================================================================
    # 4. WRITING A RUN
    #    Called by sim/run.py in this order: save_run once, then (after the
    #    truth is computed) save_observation per report, save_truth_batch on
    #    flush, and end_run if --end was given.
    # =========================================================================

    def save_run(self, run):
        """Create the ops.simulation_runs row (status 'created'). Neon picks the id; returns it.

        run.py passes a locally made uuid in run['id'], but the backend generates its own
        (gen_random_uuid()), so the simulator adopts Neon's id. Every later row (observations,
        truth rows' scenario_id) uses this returned id, which keeps Neon and the truth in sync.
        """
        row = self._call('POST', '/runs', {'map_version_id': run['map_version_id'], 'seed': run['seed'],
                                           'hazard_types': run['hazard_types']})   # RunCreate in models.py
        self.truth_storage.save_run({**run, 'id': row['id'], 'status': row['status']})
        return row['id']

    def save_truth_batch(self, rows):
        """Truth rows (damage, road status, people, truth_links, scenario): local files only, never Neon.
        Later this becomes the Databricks writer."""
        self.truth_storage.save_truth_batch(rows)

    def save_observation(self, obs):
        """Store one report in ops.observations.

        Safe to retry: the observation id is chosen by the simulator, so sending the same report
        twice makes the backend return the stored row instead of inserting a duplicate
        (repository.save_observation, step 2). The first observation of a run also flips the run
        from 'created' to 'running'.
        """
        row = self._call('POST', f'/runs/{obs["run_id"]}/observations', observation_body(obs))
        self.truth_storage.save_observation(obs)   # local copy only once Neon has accepted it
        return row

    def end_run(self, run_id, status='completed', sim_time_s=None):
        """Mark the run 'completed' (or 'failed') and stamp ended_at (POST /runs/{id}/end).

        After this the backend refuses any more writes to the run with a 409, including the
        agents' incidents, decisions and road restrictions, so only end a run when everyone is done.
        """
        row = self._call('POST', f'/runs/{run_id}/end', {'status': status, 'sim_time_s': sim_time_s})
        self.truth_storage.end_run(run_id, status, sim_time_s)   # local copy only once Neon has ended it
        return row
