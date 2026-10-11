"""The simulator as a small local web service, so the map can ask for an earthquake.

    cd simulation && ../.venv/bin/python -m uvicorn sim.server:app --port 8100

    POST /runs              {"seed": 42, "mw": 7.3}   both optional -> a new run and its truth
    GET  /runs/{id}/truth   the same truth again (operator reveal-truth only)
    GET  /runs/{id}/reports the run's reports in arrival order, each with its answer key (operator only)
    GET  /runs/{id}/dispatch units sent by a stand-in dispatcher (sim/standin.py), for designing the map
    GET  /clock             the live run's clock: simulated time, speed, reports released so far
    POST /clock             {"speed": 10|20, "playing": true|false}
    POST /stop              stop the live run and close it
    GET  /units             every responder unit: where it is, where it has been, where it is going
    GET  /agents            whether the agents are running for the live run, and the end of their log

Tools for the response agents. They answer from what is *believed* about the roads (ops.road_restrictions
plus anything units have found), never from the truth:
    POST /tools/nearest-units   {building_id | lon+lat, type?, k?}   free units that can get there soonest
    POST /tools/route           {unit_id | from_lon+from_lat, building_id | lon+lat}   how to get there
    POST /dispatch              {unit_id, building_id | lon+lat, incident_id?}         send the unit
Each takes an optional run_id and refuses if that is not the run being played.

SIM_AGENTS=1 (set by ./dev.sh) starts the agents for every new run: Generate also launches
`python -m agents.orchestrator <run_id> --watch`, and stops it when the run is stopped or replaced. Their
output goes to simulation/runs/<run_id>/agents.log. They need the SSH tunnel to the model to be open.

This serves hidden ground truth and has no login: keep it on localhost. Agents must never call it.

With BACKEND_URL set (e.g. http://127.0.0.1:8000), the map comes from Neon through the backend and every
new run is stored there. POST /runs returns success only after the initial resources and readiness
receipt commit together. Prepared submissions survive restart and can be retried.
Reports are not part of that snapshot: the live loop (sim/live.py) releases each one when the run's
clock reaches it. One run is live at a time; starting another stops and closes the one before.
The default is Neon mode; SIM_LOCAL_ONLY=1 explicitly selects a local-only developer demo.

SIM_GEMINI=1 has Gemini write the report wording (needs GEMINI_API_KEY; see sim/report_writer.py). It adds up to
a few minutes to POST /runs on the free tier; reports Gemini cannot word keep their template text. Off by default.
"""
import asyncio, collections, json, logging, os, pathlib, random, subprocess, sys, time, uuid
from types import SimpleNamespace
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from .api_storage import ApiStorage
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from . import databricks_sink
from .export import run_truth
from .live import DEFAULT_SPEED, SPEEDS, LiveRun
from .routing import Router
from .run import start_run
from .scenario import MW_RANGE
from .standin import standin_dispatch, UNITS_PER_FACILITY
from .submission import atomic_json, read_job
from .units import DispatchError, Fleet
from .storage import LocalStorage

KEEP_RUNS = 20   # truth of the most recent runs stays in memory
BACKEND_URL = None if os.environ.get('SIM_LOCAL_ONLY') == '1' else os.environ.get('BACKEND_URL', 'http://127.0.0.1:8000')
log = logging.getLogger('uvicorn.error')
AUTO_DISPATCH = os.environ.get('SIM_AUTO_DISPATCH') == '1'   # stand-in dispatcher for testing without agents
BELIEFS_TTL_S = 3.0
AGENTS = os.environ.get('SIM_AGENTS') == '1'   # start the agent pipeline for each new run
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GEMINI = os.environ.get('SIM_GEMINI') == '1'   # opt-in Gemini wording for every new run

@asynccontextmanager
async def lifespan(app):
    # Prepared submissions are retried after restart. They are already on disk.
    task = asyncio.create_task(asyncio.to_thread(ApiStorage(BACKEND_URL).recover_prepared)) if BACKEND_URL else None
    databricks_sink.upload_pending_in_background()   # truth of runs that never reached Databricks
    yield
    stop_agents()
    if task:
        await task


app = FastAPI(title='Shinjuku simulator', version='0.2.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origin_regex=r'http://(localhost|127\.0\.0\.1):\d+',
                   allow_methods=['GET', 'POST'], allow_headers=['Content-Type'])
_state = {'storage': None, 'map': None, 'router': None, 'runs': collections.OrderedDict(), 'reports': {}, 'dispatch': {},
          'live': None, 'live_storage': None, 'fleet': None, 'beliefs': (0.0, {}), 'agents': None}


def _map():
    if _state['map'] is None:   # loaded on first use: about a second (a few from Neon)
        _state['storage'] = ApiStorage(BACKEND_URL) if BACKEND_URL else LocalStorage()
        _state['map'] = _state['storage'].load_static_map()
    return _state['storage'], _state['map']


def initial_resources(static_map, run_id):
    """Modeled responder inventory, not inferred facility capacity or hidden damage."""
    types = {'Engine':'fire_engine', 'Ambulance':'ambulance', 'Patrol':'police'}
    resources = []
    for facility in sorted(static_map.facilities.values(), key=lambda f:f['id']):
        label, count = UNITS_PER_FACILITY.get(facility['type'],(None,0))
        for index in range(count):
            resources.append({'id':str(uuid.uuid5(uuid.UUID(run_id),f"{facility['id']}:{index}")),
                              'type':types[label], 'name':f"{facility['name_en'] or facility['id']} {label} {index+1}",
                              'status':'available', 'home_facility_id':facility['id'],
                              'location':{'longitude':facility['lon'],'latitude':facility['lat']},
                              'capacity':None,'skills':None,'sim_time_s':0})
    return resources


def submission_error(storage, exc):
    run_id = storage.active_run_id if isinstance(storage,ApiStorage) else None
    retryable = False
    if run_id:
        retryable = read_job(storage.truth_storage.runs_dir,run_id)['phase'] == 'prepared'
    return HTTPException(503, detail={'message':'Could not confirm saving to Neon. No run is being shown as ready.',
                                      'run_id':run_id,'retryable':retryable})


def saved_view(run_id, name):
    try:
        # encoding='utf-8' for Windows (Hasita's laptop): saved views hold Japanese report text; same as macOS/Linux.
        return json.loads((LocalStorage().runs_dir / str(uuid.UUID(run_id)) / name).read_text(encoding='utf-8'))
    except (FileNotFoundError,ValueError):
        raise HTTPException(404,'Run view not found')


def stop_agents():
    """Stop the agent pipeline of the previous run, if it is still going."""
    agents, _state['agents'] = _state['agents'], None
    if agents and agents['process'].poll() is None:
        agents['process'].terminate()
        try:
            agents['process'].wait(timeout=5)
        except subprocess.TimeoutExpired:
            agents['process'].kill()


def start_agents(run_id):
    """Start the agents on a run that is saved and playing: the orchestrator, watching for new reports.
    Only in Neon mode (agents read and write Neon), and not next to the stand-in dispatcher."""
    stop_agents()
    if not (AGENTS and BACKEND_URL) or AUTO_DISPATCH:
        return
    log_path = LocalStorage().runs_dir / run_id / 'agents.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.Popen([sys.executable, '-m', 'agents.orchestrator', run_id, '--watch'], cwd=REPO_ROOT,
                                   env={**os.environ, 'BACKEND_URL': BACKEND_URL, 'PYTHONUNBUFFERED': '1'},
                                   stdout=open(log_path, 'w', encoding='utf-8'), stderr=subprocess.STDOUT)
    except OSError as exc:   # the run plays on without agents
        log.warning('run %s: agents could not be started (%s)', run_id, exc)
        return
    _state['agents'] = {'run_id': run_id, 'process': process, 'log': log_path}


def stop_live():
    """Stop the clock of the run that is live, if any, and close it. One run plays at a time."""
    live, storage = _state['live'], _state['live_storage']
    _state['live'] = _state['live_storage'] = None
    stop_agents()
    if live is None:
        return None
    live.stop()
    _state['fleet'] = None
    databricks_sink.upload_in_background(live.run_id)   # the truth gained rows while the run played
    try:
        storage.end_run(live.run_id, sim_time_s=live.state()['sim_time_s'])
    except Exception as exc:   # closing is tidying up; never let it block the next earthquake
        log.warning('run %s: could not be closed (%s)', live.run_id, exc)
    return live.state()


def _beliefs():
    """Road status the agents believe for the live run, re-read every few seconds."""
    live, storage = _state['live'], _state['live_storage']
    at, known = _state['beliefs']
    if live is None or storage is None:
        return {}
    if time.monotonic() - at > BELIEFS_TTL_S:
        try:
            known = storage.load_road_restrictions(live.run_id)
        except Exception as exc:   # keep planning on what was last known
            log.warning('road beliefs could not be refreshed (%s)', exc)
        _state['beliefs'] = (time.monotonic(), known)
    return known


def start_live(run_id, reports, speed, static_map=None, truth=None, auto_dispatch=None):
    """Start the clock for a run. Reports and unit state go to the same place the run was created:
    Neon through the backend, or local files. With a map and the truth, the run also gets its units."""
    stop_live()
    storage = ApiStorage(BACKEND_URL) if BACKEND_URL else _map()[0]
    fleet, schedule, on_due = None, None, None
    if static_map is not None and truth is not None and getattr(static_map, 'graph', None) is not None:
        if not isinstance(_state['router'], Router):
            _state['router'] = Router(static_map.graph)
        fleet = Fleet(run_id, static_map, _state['router'], truth, initial_resources(static_map, run_id), beliefs=_beliefs)
        if auto_dispatch:
            schedule, on_due = [(u['depart_s'], u['building_id']) for u in auto_dispatch], _standin_send
    _state['live_storage'], _state['fleet'], _state['beliefs'] = storage, fleet, (0.0, {})
    _state['live'] = LiveRun(run_id, reports, storage, speed=speed, fleet=fleet, schedule=schedule, on_due=on_due).start()
    start_agents(run_id)
    return _state['live'].state()


def _standin_send(building_id, now_s):
    """Stand-in for the agents (SIM_AUTO_DISPATCH=1): send the nearest free unit. Not used when agents decide."""
    fleet = _state['fleet']
    try:
        nearest = fleet.nearest(building_id=building_id, k=1)
        if nearest:
            fleet.dispatch(nearest[0]['unit_id'], now_s, building_id=building_id)
    except DispatchError as exc:
        log.info('stand-in dispatch skipped: %s', exc)


def _live_fleet(run_id=None):
    live, fleet = _state['live'], _state['fleet']
    if live is None or fleet is None:
        raise HTTPException(409, 'No run is being played. Generate an earthquake first.')
    if run_id is not None and str(run_id) != live.run_id:
        raise HTTPException(409, f'Run {run_id} is not the run being played ({live.run_id}).')
    return live, fleet


class Place(BaseModel):
    """A destination: a building on the map, or a point."""
    run_id: str | None = None
    building_id: str | None = None
    lon: float | None = Field(default=None, ge=-180, le=180)
    lat: float | None = Field(default=None, ge=-90, le=90)


class NearestUnits(Place):
    type: str | None = Field(default=None, description='fire_engine, ambulance or police; omit for any')
    k: int = Field(default=3, ge=1, le=20)


class RouteQuestion(Place):
    unit_id: str | None = None
    from_lon: float | None = Field(default=None, ge=-180, le=180)
    from_lat: float | None = Field(default=None, ge=-90, le=90)


class Dispatch(Place):
    unit_id: str
    incident_id: str | None = None


class ClockChange(BaseModel):
    speed: int | None = Field(default=None, description=f'one of {SPEEDS}')
    playing: bool | None = None


class NewRun(BaseModel):
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1, description='omit for a random earthquake')
    mw: float | None = Field(default=None, ge=MW_RANGE[0], le=MW_RANGE[1], description='omit to let the seed pick the magnitude')
    speed: int = Field(default=DEFAULT_SPEED, description=f'simulated seconds per real second: one of {SPEEDS}')
    paused: bool = Field(default=False, description='create the run with its clock held at T+0 until POST /clock {playing: true}')


@app.get('/health')
def health():
    return {'status':'ok','magnitude_range':list(MW_RANGE),'storage_mode':'neon' if BACKEND_URL else 'local-demo',
            'speeds':list(SPEEDS),'truth_upload':'databricks' if databricks_sink.enabled() else 'off',
            'report_wording':'gemini' if GEMINI else 'templates',
            'agents':'auto-start' if AGENTS and BACKEND_URL and not AUTO_DISPATCH else 'off'}



@app.post('/runs', status_code=201)
def new_run(body: NewRun):
    """The seed decides depth, epicentre and every damage roll; mw only overrides the magnitude."""
    if body.speed not in SPEEDS:
        raise HTTPException(422, f'speed must be one of {list(SPEEDS)}')
    storage, static_map = _map()
    if BACKEND_URL:   # one durable submission per run; save the complete snapshot before success
        storage = ApiStorage(BACKEND_URL, defer_observations=True)
    seed = body.seed if body.seed is not None else random.SystemRandom().randrange(1, 2**31)
    try:
        writer = None
        if GEMINI:
            from .report_writer import write_reports as writer
        truth, sc, _ = start_run(seed, storage=storage, static_map=static_map, mw=body.mw, writer=writer,
                                 release_reports=False)

        if BACKEND_URL:
            storage.prepare_ingestion(initial_resources(static_map,truth.run_id))
        data = run_truth(truth,sc,static_map)
        if isinstance(storage,ApiStorage):
            atomic_json(storage.truth_storage.runs_dir/truth.run_id/'truth_view.json',data)
        if _state['router'] is None:
            _state['router'] = Router(static_map.graph)
        report_view = [_with_answer(item) for item in truth.reports]
        dispatch_view = standin_dispatch(static_map,truth,_state['router'],truth.reports)
        if BACKEND_URL:
            root = storage.truth_storage.runs_dir/truth.run_id
            atomic_json(root/'reports_view.json',report_view)
            atomic_json(root/'dispatch_view.json',dispatch_view)
            storage.post_pending_observations()
            data['ingestion'] = read_job(storage.truth_storage.runs_dir,truth.run_id)['receipt']
        else:
            data['ingestion'] = {'ready':False,'mode':'local-demo'}
        _state['runs'][truth.run_id] = data
        _state['reports'][truth.run_id] = report_view
        _state['dispatch'][truth.run_id] = dispatch_view
        # Only now, with the run confirmed saved, does its clock start and the previous run's stop.
        data['clock'] = start_live(truth.run_id, truth.reports, body.speed, static_map, truth,
                                   auto_dispatch=dispatch_view if AUTO_DISPATCH else None)
        if body.paused:   # held at the start: nothing is released and nobody moves until Start is pressed
            data['clock'] = _state['live'].set(playing=False)
    except Exception as exc:
        log.error('Simulation submission failed: %s',exc)
        raise submission_error(storage,exc) from exc
    databricks_sink.upload_in_background(truth.run_id)   # truth files are complete; never blocks this response
    while len(_state['runs']) > KEEP_RUNS:
        old, _ = _state['runs'].popitem(last=False)
        _state['reports'].pop(old, None)
        _state['dispatch'].pop(old, None)
    return data


def _with_answer(item):
    """A report as agents see it, plus what is really behind it. The 'answer' part is hidden truth."""
    recipe, links = item['recipe'], item['links']
    return {**item['observation'], 'answer': {
        'match_type': recipe['match_type'], 'kind': recipe['kind'], 'is_repost': recipe['is_repost'],
        'building_id': next((l['truth_entity_id'] for l in links if l['truth_entity_type'] == 'building'), None),
        'road_ids': [l['truth_entity_id'] for l in links if l['truth_entity_type'] == 'road'],
        'people': recipe['people']}}


@app.get('/runs/{run_id}/reports')
def reports(run_id: str):
    if run_id not in _state['reports']:
        return saved_view(run_id,'reports_view.json')
    return _state['reports'][run_id]


@app.get('/runs/{run_id}/dispatch')
def dispatch(run_id: str):
    if run_id not in _state['dispatch']:
        return saved_view(run_id,'dispatch_view.json')
    return _state['dispatch'][run_id]


@app.get('/runs/{run_id}/truth')
def truth(run_id: str):
    if run_id not in _state['runs']:
        data = saved_view(run_id,'truth_view.json')
        try:
            job = read_job(LocalStorage().runs_dir,str(uuid.UUID(run_id)))
            if job['phase'] == 'saved':
                data['ingestion'] = job['receipt']
        except FileNotFoundError:
            pass
        return data
    return _state['runs'][run_id]


@app.get('/clock')
def clock():
    """The live run's clock. {'run_id': None} when nothing is playing."""
    live = _state['live']
    return live.state() if live else {'run_id': None, 'finished': True, 'playing': False}


@app.post('/clock')
def change_clock(body: ClockChange):
    live = _state['live']
    if live is None:
        raise HTTPException(409, 'No run is live')
    if body.speed is not None and body.speed not in SPEEDS:
        raise HTTPException(422, f'speed must be one of {list(SPEEDS)}')
    return live.set(speed=body.speed, playing=body.playing)


@app.get('/agents')
def agents():
    """Whether the agents are running for the live run, and the last lines they printed."""
    a = _state['agents']
    if a is None:
        return {'run_id': None, 'running': False, 'enabled': AGENTS and bool(BACKEND_URL) and not AUTO_DISPATCH, 'log': []}
    try:
        lines = a['log'].read_text(encoding='utf-8', errors='replace').splitlines()[-40:]
    except OSError:
        lines = []
    return {'run_id': a['run_id'], 'running': a['process'].poll() is None, 'enabled': True, 'log': lines}


@app.get('/units')
def units():
    """Every unit in the live run, for the map: position, trail, planned route, the decision it follows."""
    fleet = _state['fleet']
    return fleet.view() if fleet else []


@app.post('/tools/nearest-units')
def nearest_units(body: NearestUnits):
    """Resource agent: which free units can reach this place soonest, on the roads believed open."""
    _, fleet = _live_fleet(body.run_id)
    try:
        return fleet.nearest(body.lon, body.lat, body.building_id, type=body.type, k=body.k)
    except DispatchError as exc:
        raise HTTPException(422, str(exc))


@app.post('/tools/route')
def route(body: RouteQuestion):
    """Routing: how a unit (or a point) would get to a place. Sends nobody."""
    _, fleet = _live_fleet(body.run_id)
    try:
        return fleet.plan_route(body.unit_id, body.from_lon, body.from_lat, body.lon, body.lat, body.building_id)
    except DispatchError as exc:
        raise HTTPException(422, str(exc))


@app.post('/dispatch', status_code=201)
def dispatch_unit(body: Dispatch):
    """Plan agent: send this unit there. Returns the decision it now follows, or 409 with the reason
    it cannot go (not free, or no way there)."""
    live, fleet = _live_fleet(body.run_id)
    try:
        return fleet.dispatch(body.unit_id, live.state()['sim_time_s'], lon=body.lon, lat=body.lat,
                              building_id=body.building_id, incident_id=body.incident_id)
    except DispatchError as exc:
        raise HTTPException(409, str(exc))


@app.post('/stop')
def stop():
    """Stop the live run and close it (the map's Reset). Safe to call when nothing is playing."""
    return stop_live() or {'run_id': None, 'finished': True, 'playing': False}


@app.post('/runs/{run_id}/retry')
def retry_submission(run_id: str):
    if not BACKEND_URL:
        raise HTTPException(409,'Neon connection is required')
    try:
        run_id = str(uuid.UUID(run_id))
        storage = ApiStorage(BACKEND_URL)
        data = saved_view(run_id,'truth_view.json')
        data['ingestion'] = storage.resume(run_id)
        _state['runs'][run_id] = data
        # The save went through this time, so the run's clock starts now, as it would have at Generate.
        reports = [{'observation': {k: v for k, v in r.items() if k != 'answer'}} for r in saved_view(run_id, 'reports_view.json')]
        roads = {r: {'status': v['status'], 'sim_time_s': 0.0} for r, v in data.get('road_status', {}).items()}
        truth = SimpleNamespace(road_status=roads, add_link=lambda *a: None)   # enough for units to drive on
        data['clock'] = start_live(run_id, reports, DEFAULT_SPEED, _map()[1], truth)
        return data
    except HTTPException:
        raise
    except (ValueError,FileNotFoundError):
        raise HTTPException(404,'Saved submission not found')
    except Exception as exc:
        raise HTTPException(503,detail={'message':'Saving to Neon still failed; the submission is retained.',
                                       'run_id':run_id,'retryable':True}) from exc
