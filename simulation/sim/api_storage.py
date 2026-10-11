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
import json, urllib.error, urllib.request, time, uuid
from pathlib import Path
from .submission import append_report, atomic_json, lock_for, read_job, write_job   # urllib is Python's built-in HTTP client: no extra package needed
from shapely.geometry import shape          # turns GeoJSON geometry dicts into shapely shapes (the simulator does geometry math on them)
from .storage import LocalStorage, build_graph, make_static_map   # build_graph is re-exported for callers of this module


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
    def __init__(self, base_url='http://127.0.0.1:8000', truth_storage=None, timeout=120, defer_observations=False):
        # base_url: where uvicorn is serving the backend (no trailing slash, so paths join cleanly).
        # timeout: seconds to wait per request; the bulk buildings layer is ~6 MB, so it is generous.
        self.base_url, self.timeout = base_url.rstrip('/'), timeout
        # Where everything that must NOT go to Neon is written (truth rows), plus a local copy of the
        # run and its observations so scripts/check_sim_run.py can compare Neon against them.
        self.truth_storage = truth_storage or LocalStorage()
        # defer_observations=True: spool reports durably while generation runs.
        # The server seals the snapshot, then submits it atomically before answering.
        self.defer_observations, self.pending_observations = defer_observations, []
        self.active_run_id = None

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
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                if e.code not in (429, 502, 503, 504) or attempt == 2:
                    raise ApiError(f'{method} {path} -> {e.code}: {detail}') from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise ApiError(f'{method} {path}: backend unavailable; submission retained for retry') from None
            time.sleep(0.5 * (2 ** attempt))

    # =========================================================================
    # 3. READING THE MAP
    #    The simulator needs every building, road and facility to compute damage.
    #    Instead of local GeoJSON files (LocalStorage), it downloads them from
    #    Neon's static.* tables through the backend's bulk map endpoints.
    # =========================================================================

    def load_static_map(self, version='shinjuku-v1'):
        """The map from Neon static.* via the API, in the same StaticMap shape LocalStorage returns,
        including the road graph (built from road_segments, see build_graph)."""
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

        road_geoms = {x['properties']['id']: shape(x['geometry']) for x in r}
        # StaticMap (sim/storage.py) keeps properties and geometry in separate dicts, all keyed by id
        # (building id bldg_<uuid>, road id u_v_key, facility id) -- the ids shared across map, graph and DB.
        # make_static_map (sim/storage.py) puts everything in a fixed order and rounds Postgres REAL values,
        # so a seed gives the same earthquake from Neon as from local files. It also builds the road graph.
        return make_static_map(
            version,
            {x['properties']['id']: x['properties'] for x in b},           # buildings: id -> columns
            {x['properties']['id']: shape(x['geometry']) for x in b},      # buildings: id -> Polygon
            {x['id']: {**{k: v for k, v in x.items() if k != 'geometry'},  # facilities: id -> columns + lon/lat
                       'lon': x['geometry']['coordinates'][0], 'lat': x['geometry']['coordinates'][1]} for x in f},
            roads,                                                         # roads: id -> columns
            road_geoms)                                                    # roads: id -> LineString

    # =========================================================================
    # 4. WRITING A RUN
    #    Called by sim/run.py in this order: save_run once, then (after the
    #    truth is computed) save_observation per report, save_truth_batch on
    #    flush, and end_run if --end was given.
    # =========================================================================

    def save_run(self, run):
        """Persist a stable run identity locally, then create it idempotently in Neon.

        A lost HTTP response can be retried with this same UUID; no second run
        is created. Truth, observations and resources share that identity.
        """
        run_id = str(run['id'])
        body = {'id': run_id, 'map_version_id':run['map_version_id'],
                'seed':run['seed'], 'hazard_types':run['hazard_types']}
        self.active_run_id = run_id
        write_job(self.truth_storage.runs_dir, {
            'run_id':run_id, 'create_body':body, 'phase':'preparing', 'last_error':None})
        row = self._call('POST','/runs',body)
        if row['id'] != run_id:
            raise ApiError('Backend must support stable run IDs; restart the updated backend')
        self.truth_storage.save_run({**run,'id':run_id,'status':row['status']})
        return run_id

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
        if self.defer_observations:
            append_report(self.truth_storage.runs_dir, obs)
            self.pending_observations.append(obs)
            return None
        row = self._call('POST', f'/runs/{obs["run_id"]}/observations', observation_body(obs))
        self.truth_storage.save_observation(obs)   # local copy only once Neon has accepted it
        return row

    def prepare_ingestion(self, resources=None):
        run_id = self.active_run_id
        job = read_job(self.truth_storage.runs_dir,run_id)
        job.update(phase='prepared', payload={
            'observations':[observation_body(o) for o in self.pending_observations],
            'resources':resources or []}, last_error=None)
        write_job(self.truth_storage.runs_dir,job)
        return job

    def post_pending_observations(self):
        if not self.active_run_id:
            raise ApiError('Create a run before submitting reports')
        job = read_job(self.truth_storage.runs_dir,self.active_run_id)
        if job['phase'] == 'preparing':
            self.prepare_ingestion()
        receipt = self.resume(self.active_run_id)
        self.pending_observations.clear()
        return receipt['stored_observations']

    def resume(self, run_id):
        root = self.truth_storage.runs_dir
        with lock_for(run_id):
            job = read_job(root,run_id)
            if job['phase'] not in ('prepared','saved'):
                raise ApiError('Generation was interrupted before the snapshot was complete; this run is not agent-ready')
            try:
                self._call('POST','/runs',job['create_body'])
                receipt = self._call('POST',f'/runs/{run_id}/ingest',job['payload'])
                if not receipt.get('ready'):
                    raise ApiError('Backend did not confirm a committed snapshot')
                job.update(phase='saved',receipt=receipt,last_error=None)
                write_job(root,job)
                # Rebuild the informational local mirror: retries do not duplicate lines.
                # Keep the simulator's original row-shaped records for check_sim_run.py.
                spool = Path(root)/str(run_id)/'pending_observations.jsonl'
                if spool.exists():
                    original = {o['id']:o for o in (json.loads(x) for x in spool.read_text().splitlines())}
                    target = Path(root)/str(run_id)/'observations.jsonl'
                    # Atomic JSON job remains the recovery source; mirror is informational.
                    target.write_text(''.join(json.dumps(o,ensure_ascii=False)+'\n' for o in original.values()))
                return receipt
            except Exception as exc:
                job.update(phase='prepared',last_error=str(exc))
                write_job(root,job)
                raise

    def recover_prepared(self):
        results = []
        for path in Path(self.truth_storage.runs_dir).glob('*/submission.json'):
            job = json.loads(path.read_text())
            if job['phase'] == 'prepared':
                try:
                    results.append(self.resume(job['run_id']))
                except Exception:
                    # Error persisted by resume; manual retry stays available.
                    pass
        return results

    def end_run(self, run_id, status='completed', sim_time_s=None):
        """Mark the run 'completed' (or 'failed') and stamp ended_at (POST /runs/{id}/end).

        After this the backend refuses any more writes to the run with a 409, including the
        agents' incidents, decisions and road restrictions, so only end a run when everyone is done.
        """
        row = self._call('POST', f'/runs/{run_id}/end', {'status': status, 'sim_time_s': sim_time_s})
        self.truth_storage.end_run(run_id, status, sim_time_s)   # local copy only once Neon has ended it
        return row
