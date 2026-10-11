"""ApiStorage against a stand-in backend (no Neon needed). The real round trip is scripts/check_sim_run.py."""
import json, threading, uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from sim.api_storage import ApiError, ApiStorage, observation_body
from sim.storage import LocalStorage

OBS = {'id': str(uuid.uuid4()), 'run_id': None, 'channel': 'social_media', 'source_reliability': 0.3,
       'format': 'text', 'lang': 'en', 'original_text': 'x', 'text_en': 'x', 'media_url': None,
       'claimed_location': 'near the station', 'claimed_geom': {'type': 'Point', 'coordinates': [139.7, 35.69]},
       'location_precision': 'gps', 'sim_time_s': 12.5, 'received_at': '2026-10-10T00:00:00+00:00'}


def test_observation_body_matches_api_model():
    body = observation_body(OBS)
    assert body['location'] == {'longitude': 139.7, 'latitude': 35.69}
    assert 'claimed_geom' not in body and 'run_id' not in body and 'received_at' not in body
    assert observation_body({**OBS, 'claimed_geom': None})['location'] is None


@pytest.fixture
def backend():
    calls, server_run_id = [], str(uuid.uuid4())

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, body))
            if body.get('channel') == 'bad':
                status, out = 422, {'detail': 'bad channel'}
            elif self.path == '/runs':
                status, out = 201, {'id': body['id'], 'status': 'created'}
            elif self.path.endswith('/ingest'):
                status, out = 200, {'ready':True,'stored_observations':len(body['observations']),
                                    'stored_resources':len(body['resources'])}
            else:
                status, out = 200, {'id': body.get('id'), 'status': body.get('status')}
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())

        def log_message(self, *_):
            pass

    srv = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_port}', calls, server_run_id
    srv.shutdown()


def test_run_id_comes_from_the_backend(backend, tmp_path):
    url, calls, server_run_id = backend
    s = ApiStorage(url, truth_storage=LocalStorage(runs_dir=tmp_path))
    run_id = s.save_run({'id': server_run_id, 'map_version_id': 'shinjuku-v1', 'seed': 42,
                         'hazard_types': ['earthquake'], 'status': 'running'})
    assert run_id == server_run_id
    assert calls[0] == ('/runs', {'id':server_run_id, 'map_version_id': 'shinjuku-v1', 'seed': 42, 'hazard_types': ['earthquake']})
    s.save_observation({**OBS, 'run_id': run_id})
    assert calls[1][0] == f'/runs/{run_id}/observations' and calls[1][1]['id'] == OBS['id']
    s.end_run(run_id)
    assert calls[2] == (f'/runs/{run_id}/end', {'status': 'completed', 'sim_time_s': None})
    assert json.load(open(tmp_path / run_id / 'simulation_run.json'))['status'] == 'completed'


def test_backend_errors_are_raised_with_detail(backend, tmp_path):
    url, _, _ = backend
    s = ApiStorage(url, truth_storage=LocalStorage(runs_dir=tmp_path))
    with pytest.raises(ApiError, match='422.*bad channel'):
        s.save_observation({**OBS, 'run_id': 'r', 'channel': 'bad'})


def test_graph_built_from_road_segments_routes_like_the_osmnx_one():
    from shapely.geometry import LineString
    from sim.api_storage import build_graph
    from sim.routing import Router
    a, b, c = (139.700, 35.690), (139.701, 35.690), (139.701, 35.691)
    segs = {'1_2_0': (a, b, 100.0), '2_1_0': (b, a, 100.0), '2_3_0': (b, c, 110.0), '3_2_0': (c, b, 110.0)}
    roads = {rid: {'id': rid, 'from_node': int(rid.split('_')[0]), 'to_node': int(rid.split('_')[1]),
                   'length_m': L, 'speed_kmh': 36.0} for rid, (_, _, L) in segs.items()}
    G = build_graph(roads, {rid: LineString([p, q]) for rid, (p, q, _) in segs.items()})
    assert {f'{u}_{v}_{k}' for u, v, k in G.edges(keys=True)} == set(segs)   # ids match road ids
    assert G.nodes[3]['x'] == c[0] and G.edges[1, 2, 0]['travel_time'] == 10.0   # 100 m at 10 m/s
    r = Router(G)
    assert r.node((139.7009, 35.6909)) == 3 and r.is_reachable(1, 3, {})
    assert not r.is_reachable(1, 3, {'2_3_0': 'BLOCKED'})


def test_deferred_reports_are_sent_only_when_asked(backend, tmp_path):
    url, calls, _ = backend
    s = ApiStorage(url, truth_storage=LocalStorage(runs_dir=tmp_path), defer_observations=True)
    run_id = s.save_run({'id':str(uuid.uuid4()),'map_version_id':'shinjuku-v1','seed':42,'hazard_types':['earthquake']})
    calls.clear()
    s.save_observation({**OBS, 'run_id':run_id})
    assert calls == [] and len(s.pending_observations) == 1
    assert s.post_pending_observations() == 1
    assert calls[-1][0] == f'/runs/{run_id}/ingest' and s.pending_observations == []


def test_prepared_submission_survives_failed_save_and_new_storage_instance(tmp_path):
    run_id = str(uuid.uuid4())
    class Storage(ApiStorage):
        fail = True
        def _call(self, method, path, body=None):
            if path == '/runs':
                return {'id':body['id'],'status':'created'}
            if self.fail:
                raise ApiError('network down')
            return {'ready':True,'stored_observations':len(body['observations']),'stored_resources':0}
    s = Storage('http://unused',truth_storage=LocalStorage(runs_dir=tmp_path),defer_observations=True)
    s.save_run({'id':run_id,'map_version_id':'shinjuku-v1','seed':42,'hazard_types':['earthquake']})
    s.save_observation({**OBS,'run_id':run_id})
    s.prepare_ingestion()
    with pytest.raises(ApiError):
        s.post_pending_observations()
    assert json.loads((tmp_path/run_id/'submission.json').read_text())['phase'] == 'prepared'
    restarted = Storage('http://unused',truth_storage=LocalStorage(runs_dir=tmp_path))
    restarted.fail = False
    assert restarted.recover_prepared()[0]['ready']
    assert json.loads((tmp_path/run_id/'submission.json').read_text())['phase'] == 'saved'
    assert len((tmp_path/run_id/'observations.jsonl').read_text().splitlines()) == 1
    restarted.resume(run_id)
    assert len((tmp_path/run_id/'observations.jsonl').read_text().splitlines()) == 1
