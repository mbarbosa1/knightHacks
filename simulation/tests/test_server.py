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
