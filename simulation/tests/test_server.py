import pathlib, sys
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient
from sim import server
from sim.storage import LocalStorage


@pytest.fixture(scope='module')
def client(tmp_path_factory):
    server.BACKEND_URL = None  # explicit local-demo tests, never contact Neon
    if not (pathlib.Path(server.__file__).parents[1]/'data'/'buildings.geojson').exists():
        pytest.skip('Legacy physics checks require the optional simulation/data files')
    st = LocalStorage(runs_dir=tmp_path_factory.mktemp('runs'))   # keep test runs out of simulation/runs
    server._state.update(storage=st, map=st.load_static_map())
    return TestClient(server.app)


def test_fixed_magnitude_random_everything_else(client):
    a = client.post('/runs', json={'mw': 7.0}).json()
    b = client.post('/runs', json={'mw': 7.0}).json()
    assert a['scenario']['mw'] == b['scenario']['mw'] == 7.0
    assert a['seed'] != b['seed'] and a['run_id'] != b['run_id']
    assert (a['scenario']['epicenter_lon'], a['scenario']['depth_km']) != (b['scenario']['epicenter_lon'], b['scenario']['depth_km'])
    assert a['building_damage'] != b['building_damage']


def test_same_seed_replays_and_truth_can_be_fetched_again(client):
    a = client.post('/runs', json={'seed': 42, 'mw': 7.4})
    assert a.status_code == 201
    a = a.json()
    b = client.post('/runs', json={'seed': 42, 'mw': 7.4}).json()
    assert a['building_damage'] == b['building_damage'] and a['road_status'] == b['road_status']
    assert a['summary']['buildings']['collapsed'] == 62 and a['summary']['roads']['BLOCKED'] == 48
    assert client.get(f"/runs/{a['run_id']}/truth").json() == a
    assert client.get('/runs/nope/truth').status_code == 404
    reports = client.get(f"/runs/{a['run_id']}/reports").json()
    assert len(reports) == a['summary']['reports'] == 507
    assert [r['sim_time_s'] for r in reports] == sorted(r['sim_time_s'] for r in reports)
    assert {r['answer']['match_type'] for r in reports} == {'matched', 'misleading', 'fabricated', 'unmatched'}
    assert all(r['answer']['building_id'] in a['building_damage'] for r in reports if r['answer']['building_id'])
    units = client.get(f"/runs/{a['run_id']}/dispatch").json()
    assert 5 <= len(units) <= 13 and len({u['id'] for u in units}) == len(units)
    for u in units:
        times = [p[2] for p in u['path']]
        assert times == sorted(times) and times[0] >= u['depart_s'] - 0.2
        assert u['events'][0]['kind'] == 'dispatched' and u['people'] > 0
        for e in u['events']:
            if e['kind'] == 'blocked':   # a unit only ever reports a road that really is blocked
                assert e['road_ids'] and all(a['road_status'][r]['status'] == 'BLOCKED' for r in e['road_ids'])
        assert u['rerouted'] == any(e['kind'] == 'blocked' for e in u['events'])
    assert any(u['rerouted'] for u in units) and sum(u['arrive_s'] is not None for u in units) >= len(units) - 2


def test_random_earthquake_and_limits(client):
    r = client.post('/runs', json={}).json()
    assert 5.8 <= r['scenario']['mw'] <= 8.0 and r['seed'] > 0
    assert client.post('/runs', json={'mw': 9.5}).status_code == 422
    assert client.post('/runs', json={'mw': 3.0}).status_code == 422
    assert client.get('/health').json()['magnitude_range'] == [5.8, 8.0]


def test_reports_arrive_on_the_clock_and_one_run_plays_at_a_time(client):
    import json, time
    first = client.post('/runs', json={'seed': 42, 'mw': 7.4, 'speed': 20}).json()
    assert first['clock']['run_id'] == first['run_id'] and first['clock']['reports_total'] == 507
    assert first['clock']['speed'] == 20 and first['clock']['playing']
    saved = server._state['storage'].runs_dir / first['run_id'] / 'observations.jsonl'
    assert not saved.exists()                                     # nothing is released at Generate
    time.sleep(1.8)                                               # ~36 simulated seconds at 20x
    clock = client.get('/clock').json()
    assert 20 < clock['sim_time_s'] < 80 and 0 < clock['reports_released'] < 30
    released = [json.loads(l) for l in open(saved)]
    assert len(released) == client.get('/clock').json()['reports_released'] or len(released) > 0
    assert all(r['sim_time_s'] <= client.get('/clock').json()['sim_time_s'] for r in released)
    assert [r['sim_time_s'] for r in released] == sorted(r['sim_time_s'] for r in released)

    assert client.post('/clock', json={'speed': 30}).status_code == 422
    paused = client.post('/clock', json={'playing': False, 'speed': 10}).json()
    assert paused['speed'] == 10 and not paused['playing']
    time.sleep(0.4)
    assert client.get('/clock').json()['sim_time_s'] == paused['sim_time_s']

    first_clock = server._state['live']
    second = client.post('/runs', json={'mw': 6.5}).json()         # a new earthquake stops the one before
    assert first_clock.finished and client.get('/clock').json()['run_id'] == second['run_id']
    assert second['clock']['speed'] == 10                          # the default
    run_file = json.load(open(server._state['storage'].runs_dir / first['run_id'] / 'simulation_run.json'))
    assert run_file['status'] == 'completed' and run_file['ended_at']

    assert client.post('/stop').json()['finished']
    assert client.get('/clock').json() == {'run_id': None, 'finished': True, 'playing': False}
    assert client.post('/clock', json={'playing': True}).status_code == 409
    assert client.post('/runs', json={'speed': 7}).status_code == 422


def test_agents_can_find_route_and_send_units(client):
    import json, time
    assert client.get('/units').json() == []                      # nothing is being played
    assert client.post('/tools/nearest-units', json={'lon': 139.7, 'lat': 35.69}).status_code == 409
    run = client.post('/runs', json={'seed': 42, 'mw': 7.4, 'speed': 20}).json()
    units = client.get('/units').json()
    assert len(units) == 13 and {u['status'] for u in units} == {'available'}
    assert run['clock']['units'] == {'available': 13, 'en_route': 0, 'busy': 0}

    target = next(b for b, d in run['building_damage'].items() if d['damage_level'] == 'collapsed')
    near = client.post('/tools/nearest-units', json={'building_id': target, 'type': 'fire_engine', 'k': 2}).json()
    assert len(near) == 2 and {n['type'] for n in near} == {'fire_engine'}
    assert near[0]['travel_time_s'] <= near[1]['travel_time_s'] and near[0]['skills'] == ['rescue']
    unit = near[0]['unit_id']

    route = client.post('/tools/route', json={'unit_id': unit, 'building_id': target}).json()
    assert route['reachable'] and route['road_ids'] and route['geometry']['type'] == 'LineString'
    assert abs(route['travel_time_s'] - near[0]['travel_time_s']) < 1
    assert client.get('/units').json() == units                    # asking sends nobody

    assert client.post('/tools/route', json={'building_id': target}).status_code == 422       # no start
    assert client.post('/tools/nearest-units', json={'building_id': 'nope'}).status_code == 422
    assert client.post('/dispatch', json={'unit_id': unit, 'building_id': target, 'run_id': 'other'}).status_code == 409
    assert client.post('/dispatch', json={'unit_id': 'nope', 'building_id': target}).status_code == 409

    sent = client.post('/dispatch', json={'unit_id': unit, 'building_id': target, 'run_id': run['run_id']})
    assert sent.status_code == 201
    decision = sent.json()
    assert decision['status'] == 'sent' and decision['version'] == 1 and decision['eta_s'] > 0
    assert decision['route_geojson']['properties']['road_ids']
    again = client.post('/dispatch', json={'unit_id': unit, 'building_id': target})
    assert again.status_code == 409 and 'detail' in again.json()   # already on a job
    assert unit not in {n['unit_id'] for n in client.post('/tools/nearest-units', json={'building_id': target, 'k': 20}).json()}

    start = next(u for u in units if u['id'] == unit)
    time.sleep(1.5)                                                # ~30 simulated seconds at 20x
    now = next(u for u in client.get('/units').json() if u['id'] == unit)
    assert (now['lon'], now['lat']) != (start['lon'], start['lat']) and now['destination']['building_id'] == target
    assert now['decision']['id'] == decision['id'] and len(now['trail']) >= 2
    saved = [json.loads(l) for l in open(server._state['storage'].runs_dir / run['run_id'] / 'resources.jsonl')]
    assert saved and saved[0]['status'] == 'en_route'              # its position is saved as it drives
    kept = [json.loads(l) for l in open(server._state['storage'].runs_dir / run['run_id'] / 'decisions.jsonl')]
    assert kept[0]['id'] == decision['id'] and kept[0]['status'] == 'sent' and kept[0]['assignments'] == [{'resource_id': unit}]
    assert client.post('/stop').json()['finished'] and client.get('/units').json() == []


@pytest.fixture
def neon_client(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from sim.api_storage import ApiStorage,ApiError
    import uuid
    run_id = str(uuid.uuid4())
    observations = [{'id':str(uuid.uuid4()),'run_id':run_id,'channel':'sensor','sim_time_s':1}]
    map_data = SimpleNamespace(facilities={'h':{'id':'h','type':'hospital','name_en':'Hospital',
                                               'lon':139.7,'lat':35.7}},graph=None)
    flags = {'fail':False,'committed':False}
    local = LocalStorage(runs_dir=tmp_path)
    class Storage(ApiStorage):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,truth_storage=local,**kwargs)
        def _call(self,method,path,body=None):
            if path == '/runs':
                return {'id':body['id'],'status':'created'}
            if flags['fail']:
                raise ApiError('backend down')
            flags['committed'] = True
            return {'ready':True,'stored_observations':len(body['observations']),
                    'stored_resources':len(body['resources'])}
    def generate(seed,storage,**kwargs):
        storage.save_run({'id':run_id,'map_version_id':'shinjuku-v1','seed':seed,'hazard_types':['earthquake']})
        storage.save_observation(observations[0])
        return SimpleNamespace(run_id=run_id,reports=[]),None,map_data
    monkeypatch.setattr(server,'BACKEND_URL','http://unused')
    monkeypatch.setattr(server,'ApiStorage',Storage)
    monkeypatch.setattr(server,'LocalStorage',lambda:local)
    monkeypatch.setattr(server,'_map',lambda:(None,map_data))
    monkeypatch.setattr(server,'start_run',generate)
    monkeypatch.setattr(server,'run_truth',lambda *args:{'run_id':run_id})
    monkeypatch.setattr(server,'standin_dispatch',lambda *args:[])
    monkeypatch.setitem(server._state,'router',object())
    return TestClient(server.app),flags,run_id,tmp_path


def test_neon_generation_confirms_commit_and_resources_before_success(neon_client):
    client,flags,run_id,_ = neon_client
    response = client.post('/runs',json={'mw':7.0})
    assert response.status_code == 201 and flags['committed']
    assert response.json()['ingestion'] == {'ready':True,'stored_observations':1,'stored_resources':2}


def test_failed_save_is_not_success_and_same_run_can_be_retried(neon_client):
    import json
    client,flags,run_id,root = neon_client
    flags['fail'] = True
    response = client.post('/runs',json={'mw':7.0})
    assert response.status_code == 503 and response.json()['detail']['retryable']
    assert json.loads((root/run_id/'submission.json').read_text())['phase'] == 'prepared'
    flags['fail'] = False
    retry = client.post(f'/runs/{run_id}/retry',json={})
    assert retry.status_code == 200 and retry.json()['run_id'] == run_id
    assert retry.json()['ingestion']['ready']
    assert retry.json()['clock']['run_id'] == run_id      # a run saved on retry starts its clock too
    server.stop_live()
