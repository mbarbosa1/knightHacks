"""Start a run: load the map, create the run + scenario rows, compute truth, flush.

    python -m sim.run --seed 42 [--mw 7.3]
"""
import argparse, collections, datetime, uuid
from .damage import compute_building_damage
from .damage_model import shindo_class
from .roads import compute_road_damage
from .scenario import random_scenario
from .storage import LocalStorage
from .truth import TruthState

SIMULATOR_VERSION = '0.1.0'


def start_run(seed, storage=None, static_map=None, scenario=None, map_version='shinjuku-v1', mw=None):
    storage = storage or LocalStorage()
    static_map = static_map or storage.load_static_map(map_version)
    sc = scenario or random_scenario(seed, mw=mw)
    run_id, now = str(uuid.uuid4()), datetime.datetime.now(datetime.timezone.utc).isoformat()
    storage.save_run({'id': run_id, 'map_version_id': static_map.version, 'seed': sc.seed,
                      'hazard_types': list(sc.hazard_types), 'status': 'running',
                      'started_at': now, 'ended_at': None})
    truth = TruthState(run_id)
    truth.set_scenario({'seed': sc.seed, 'hazard_types': list(sc.hazard_types), 'params': sc.params(),
                        'map_version_id': static_map.version, 'simulator_version': SIMULATOR_VERSION,
                        'started_at': now, 'ended_at': None})
    compute_building_damage(static_map, sc, truth)
    compute_road_damage(static_map, truth)
    truth.flush(storage)
    return truth, sc, static_map


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--seed', type=int, required=True)
    ap.add_argument('--mw', type=float, help='fix the magnitude instead of drawing it from the seed')
    a = ap.parse_args()
    truth, sc, _ = start_run(a.seed, mw=a.mw)
    i = [d['intensity'] for d in truth.building_damage.values()]
    print(f'run {truth.run_id}  seed {sc.seed}  {sc.params()}')
    print(f'Intensity {shindo_class(min(i))} to {shindo_class(max(i))} (Japan scale), {min(i)}-{max(i)}')
    print(dict(collections.Counter(d['damage_level'] for d in truth.building_damage.values())))
    print('roads', dict(collections.Counter(r['status'] for r in truth.road_status.values())))
