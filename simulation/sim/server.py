"""The simulator as a small local web service, so the map can ask for an earthquake.

    cd simulation && ../.venv/bin/python -m uvicorn sim.server:app --port 8100

    POST /runs              {"seed": 42, "mw": 7.3}   both optional -> a new run and its truth
    GET  /runs/{id}/truth   the same truth again (operator reveal-truth only)
    GET  /runs/{id}/reports the run's reports in arrival order, each with its answer key (operator only)
    GET  /runs/{id}/dispatch units sent by a stand-in dispatcher (sim/standin.py), for designing the map

This serves hidden ground truth and has no login: keep it on localhost. Agents must never call it.

With BACKEND_URL set (e.g. http://127.0.0.1:8000), the map comes from Neon through the backend and every
new run is stored there. POST /runs returns success only after the reports, initial resources and
readiness receipt commit together. Prepared submissions survive restart and can be retried.
The default is Neon mode; SIM_LOCAL_ONLY=1 explicitly selects a local-only developer demo.
"""
import asyncio, collections, json, logging, os, random, uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from .api_storage import ApiStorage
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from .export import run_truth
from .routing import Router
from .run import start_run
from .scenario import MW_RANGE
from .standin import standin_dispatch, UNITS_PER_FACILITY
from .submission import atomic_json, read_job
from .storage import LocalStorage

KEEP_RUNS = 20   # truth of the most recent runs stays in memory
BACKEND_URL = None if os.environ.get('SIM_LOCAL_ONLY') == '1' else os.environ.get('BACKEND_URL', 'http://127.0.0.1:8000')
log = logging.getLogger('uvicorn.error')

@asynccontextmanager
async def lifespan(app):
    # Prepared submissions are retried after restart. They are already on disk.
    task = asyncio.create_task(asyncio.to_thread(ApiStorage(BACKEND_URL).recover_prepared)) if BACKEND_URL else None
    yield
    if task:
        await task


app = FastAPI(title='Shinjuku simulator', version='0.2.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origin_regex=r'http://(localhost|127\.0\.0\.1):\d+',
                   allow_methods=['GET', 'POST'], allow_headers=['Content-Type'])
_state = {'storage': None, 'map': None, 'router': None, 'runs': collections.OrderedDict(), 'reports': {}, 'dispatch': {}}


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
        return json.loads((LocalStorage().runs_dir / str(uuid.UUID(run_id)) / name).read_text())
    except (FileNotFoundError,ValueError):
        raise HTTPException(404,'Run view not found')


class NewRun(BaseModel):
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1, description='omit for a random earthquake')
    mw: float | None = Field(default=None, ge=MW_RANGE[0], le=MW_RANGE[1], description='omit to let the seed pick the magnitude')


@app.get('/health')
def health():
    return {'status':'ok','magnitude_range':list(MW_RANGE),'storage_mode':'neon' if BACKEND_URL else 'local-demo'}


@app.post('/runs', status_code=201)
def new_run(body: NewRun):
    """The seed decides depth, epicentre and every damage roll; mw only overrides the magnitude."""
    storage, static_map = _map()
    if BACKEND_URL:   # one durable submission per run; save the complete snapshot before success
        storage = ApiStorage(BACKEND_URL, defer_observations=True)
    seed = body.seed if body.seed is not None else random.SystemRandom().randrange(1, 2**31)
    try:
        truth, sc, _ = start_run(seed, storage=storage, static_map=static_map, mw=body.mw)
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
    except Exception as exc:
        log.error('Simulation submission failed: %s',exc)
        raise submission_error(storage,exc) from exc
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
        return data
    except HTTPException:
        raise
    except (ValueError,FileNotFoundError):
        raise HTTPException(404,'Saved submission not found')
    except Exception as exc:
        raise HTTPException(503,detail={'message':'Saving to Neon still failed; the submission is retained.',
                                       'run_id':run_id,'retryable':True}) from exc
