"""The simulator as a small local web service, so the map can ask for an earthquake.

    cd simulation && ../.venv/bin/python -m uvicorn sim.server:app --port 8100

    POST /runs              {"seed": 42, "mw": 7.3}   both optional -> a new run and its truth
    GET  /runs/{id}/truth   the same truth again (operator reveal-truth only)
    GET  /runs/{id}/reports the run's reports in arrival order, each with its answer key (operator only)
    GET  /runs/{id}/dispatch units sent by a stand-in dispatcher (sim/standin.py), for designing the map

This serves hidden ground truth and has no login: keep it on localhost. Agents must never call it.

With BACKEND_URL set (e.g. http://127.0.0.1:8000), the map comes from Neon through the backend and every
new run is stored there: POST /runs creates ops.simulation_runs (the returned run_id is Neon's), answers
right away, and the reports go to ops.observations in the background. Without it, local files as before.
"""
import collections, logging, os, random
from fastapi import BackgroundTasks, FastAPI, HTTPException
from .api_storage import ApiStorage
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from .export import run_truth
from .routing import Router
from .run import start_run
from .scenario import MW_RANGE
from .standin import standin_dispatch
from .storage import LocalStorage

KEEP_RUNS = 20   # truth of the most recent runs stays in memory
BACKEND_URL = os.environ.get('BACKEND_URL')
log = logging.getLogger('uvicorn.error')

app = FastAPI(title='Shinjuku simulator', version='0.1.0')
app.add_middleware(CORSMiddleware, allow_origin_regex=r'http://(localhost|127\.0\.0\.1):\d+',
                   allow_methods=['GET', 'POST'], allow_headers=['Content-Type'])
_state = {'storage': None, 'map': None, 'router': None, 'runs': collections.OrderedDict(), 'reports': {}, 'dispatch': {}}


def _map():
    if _state['map'] is None:   # loaded on first use: about a second (a few from Neon)
        _state['storage'] = ApiStorage(BACKEND_URL) if BACKEND_URL else LocalStorage()
        _state['map'] = _state['storage'].load_static_map()
    return _state['storage'], _state['map']


def _post_reports(storage, run_id):
    """Background task: the run's reports into Neon. A failure marks the run failed instead of leaving it half-filled."""
    try:
        n = storage.post_pending_observations()
        log.info('run %s: %d reports stored in Neon', run_id, n)
    except Exception as e:
        log.error('run %s: storing reports failed (%s); marking the run failed', run_id, e)
        try:
            storage.end_run(run_id, status='failed')
        except Exception:
            pass


class NewRun(BaseModel):
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1, description='omit for a random earthquake')
    mw: float | None = Field(default=None, ge=MW_RANGE[0], le=MW_RANGE[1], description='omit to let the seed pick the magnitude')


@app.get('/health')
def health():
    return {'status': 'ok', 'magnitude_range': list(MW_RANGE)}


@app.post('/runs', status_code=201)
def new_run(body: NewRun, background: BackgroundTasks):
    """The seed decides depth, epicentre and every damage roll; mw only overrides the magnitude."""
    storage, static_map = _map()
    if BACKEND_URL:   # one storage per run, holding its reports until the background task sends them
        storage = ApiStorage(BACKEND_URL, defer_observations=True)
    seed = body.seed if body.seed is not None else random.SystemRandom().randrange(1, 2**31)
    truth, sc, _ = start_run(seed, storage=storage, static_map=static_map, mw=body.mw)
    if BACKEND_URL:
        background.add_task(_post_reports, storage, truth.run_id)   # runs after the response is sent
    data = run_truth(truth, sc, static_map)
    _state['runs'][truth.run_id] = data
    _state['reports'][truth.run_id] = [_with_answer(item) for item in truth.reports]
    if _state['router'] is None:
        _state['router'] = Router(static_map.graph)
    _state['dispatch'][truth.run_id] = standin_dispatch(static_map, truth, _state['router'], truth.reports)
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
        raise HTTPException(404, 'Run not found (only recent runs are kept in memory)')
    return _state['reports'][run_id]


@app.get('/runs/{run_id}/dispatch')
def dispatch(run_id: str):
    if run_id not in _state['dispatch']:
        raise HTTPException(404, 'Run not found (only recent runs are kept in memory)')
    return _state['dispatch'][run_id]


@app.get('/runs/{run_id}/truth')
def truth(run_id: str):
    if run_id not in _state['runs']:
        raise HTTPException(404, 'Run not found (only recent runs are kept in memory)')
    return _state['runs'][run_id]
