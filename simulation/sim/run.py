"""Start a run: load the map, create the run + scenario rows, compute truth, flush.

    python -m sim.run --seed 42 [--mw 7.3] [--gemini] [--save-reports runs/reports.json]

--gemini has Gemini write the report wording (needs GEMINI_API_KEY and GEMINI_MODEL, see sim/report_writer.py);
any report it gets wrong, or every report if Gemini is unreachable, keeps the template text.
"""
import argparse, collections, datetime, json, uuid
from .damage import compute_building_damage
from .damage_model import shindo_class
from .people import compute_people
from .reports import generate_reports
from .roads import compute_road_damage
from .scenario import random_scenario
from .storage import LocalStorage
from .truth import TruthState

SIMULATOR_VERSION = '0.1.0'


def start_run(seed, storage=None, static_map=None, scenario=None, map_version='shinjuku-v1', mw=None, writer=None,
              save_reports=None, release_reports=True):
    """writer: optional wording function for generate_reports (e.g. report_writer.write_reports).
    save_reports: optional path for every report with its recipe and links, in the shape of Gerard's sample file.
    That file holds the answer key, so it is for the simulator team only, never for agents.
    release_reports=False computes everything but saves no reports: the live loop (sim/live.py) releases
    them on the clock instead. The default releases them all at once, for the command line."""

    storage = storage or LocalStorage()
    static_map = static_map or storage.load_static_map(map_version)
    sc = scenario or random_scenario(seed, mw=mw)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # The storage may assign its own id (Neon does); everything after this uses the one it returns.
    run_id = storage.save_run({'id': str(uuid.uuid4()), 'map_version_id': static_map.version, 'seed': sc.seed,
                               'hazard_types': list(sc.hazard_types), 'status': 'running',
                               'started_at': now, 'ended_at': None})
    truth = TruthState(run_id)
    truth.set_scenario({'seed': sc.seed, 'hazard_types': list(sc.hazard_types), 'params': sc.params(),
                        'map_version_id': static_map.version, 'simulator_version': SIMULATOR_VERSION,
                        'started_at': now, 'ended_at': None})
    compute_building_damage(static_map, sc, truth)
    compute_road_damage(static_map, truth)
    compute_people(static_map, sc, truth)
    start = datetime.datetime.fromisoformat(now)
    truth.reports = generate_reports(static_map, sc, truth, writer=writer)
    if save_reports:
        with open(save_reports, 'w', encoding='utf-8') as f:
            json.dump({'scenario': sc.params(), 'reports': truth.reports}, f, ensure_ascii=False, indent=1)
    for item in truth.reports:

        obs = item['observation']
        if release_reports:   # all at once, stamped with the time each would have arrived
            storage.save_observation({**obs, 'received_at': (start + datetime.timedelta(seconds=obs['sim_time_s'])).isoformat()})
        for link in item['links']:
            truth.add_link(obs['id'], link['match_type'], link['truth_entity_type'], link['truth_entity_id'],
                           link['truth_simulation_time_s'])
    truth.flush(storage)
    return truth, sc, static_map


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--seed', type=int, required=True)
    ap.add_argument('--mw', type=float, help='fix the magnitude instead of drawing it from the seed')
    ap.add_argument('--api', help='backend URL (e.g. http://127.0.0.1:8000): read the map from Neon and write the run there')
    ap.add_argument('--end', action='store_true', help='mark the run completed afterwards (no more writes, agents included)')
    ap.add_argument('--gemini', action='store_true', help='have Gemini write the report wording (opt-in)')
    ap.add_argument('--save-reports', metavar='PATH', help='also write reports + recipes + answer key to a JSON file')
    a = ap.parse_args()
    storage = None
    if a.api:
        from .api_storage import ApiStorage
        storage = ApiStorage(a.api)
    writer = None
    if a.gemini:
        from .report_writer import write_reports as writer
    truth, sc, _ = start_run(a.seed, storage=storage, mw=a.mw, writer=writer, save_reports=a.save_reports)
    if a.end:
        (storage or LocalStorage()).end_run(truth.run_id)
    i = [d['intensity'] for d in truth.building_damage.values()]

    print(f'run {truth.run_id}  seed {sc.seed}  {sc.params()}')
    print(f'Intensity {shindo_class(min(i))} to {shindo_class(max(i))} (Japan scale), {min(i)}-{max(i)}')
    print(dict(collections.Counter(d['damage_level'] for d in truth.building_damage.values())))
    print('roads', dict(collections.Counter(r['status'] for r in truth.road_status.values())))
    people = collections.Counter()
    for g in truth.people.values():
        people[g['condition']] += g['count']
    print('people', dict(people))
    print('reports', len({l['observation_id'] for l in truth.truth_links}),
          dict(collections.Counter(l['match_type'] for l in truth.truth_links)))
