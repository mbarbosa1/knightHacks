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
                status, out = 201, {'id': server_run_id, 'status': 'created'}
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
    run_id = s.save_run({'id': 'local-id', 'map_version_id': 'shinjuku-v1', 'seed': 42,
                         'hazard_types': ['earthquake'], 'status': 'running'})
    assert run_id == server_run_id
    assert calls[0] == ('/runs', {'map_version_id': 'shinjuku-v1', 'seed': 42, 'hazard_types': ['earthquake']})
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
