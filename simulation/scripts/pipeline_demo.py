"""The whole simulator -> Neon pipeline, one step at a time, printing what each step did.

usage (from simulation/, with the backend running):
    ../.venv/bin/python scripts/pipeline_demo.py --seed 7 [--api http://127.0.0.1:8000] [--mw 7.3]
then check it independently against Neon (from the repo root):
    backend/.venv/bin/python scripts/check_sim_run.py <run_id>

Does the same as sim.run.start_run, split into steps so each one can be seen:
  1. map from Neon        GET /map/versions/{v}/buildings|roads|facilities
  2. event + hidden truth  scenario, building damage, road damage, people (stays in the simulator)
  3. reports into ops      POST /runs, then POST /runs/{id}/observations per report
"""
import argparse, collections, datetime, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sim.api_storage import ApiStorage
from sim.damage import compute_building_damage
from sim.damage_model import shindo_class
from sim.people import compute_people
from sim.reports import generate_reports
from sim.roads import compute_road_damage
from sim.run import SIMULATOR_VERSION
from sim.scenario import random_scenario
from sim.truth import TruthState

ap = argparse.ArgumentParser()
ap.add_argument('--seed', type=int, required=True)
ap.add_argument('--mw', type=float)
ap.add_argument('--api', default='http://127.0.0.1:8000')
ap.add_argument('--map-version', default='shinjuku-v1')
a = ap.parse_args()
storage = ApiStorage(a.api)

# --- 1. the map, from Neon static.* -------------------------------------------------------------
t = time.time()
m = storage.load_static_map(a.map_version)
print(f'1. map {m.version} from Neon: {len(m.buildings)} buildings, {len(m.roads)} roads, '
      f'{len(m.facilities)} facilities ({time.time() - t:.1f} s)')

# --- 2. the event and its hidden truth (nothing leaves the simulator yet) -----------------------
sc = random_scenario(a.seed, mw=a.mw)
print(f'2. earthquake from seed {sc.seed}: Mw {sc.mw}, depth {sc.depth_km} km, '
      f'epicentre ({sc.epicenter_lon}, {sc.epicenter_lat})')
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
run_id = storage.save_run({'id': None, 'map_version_id': m.version, 'seed': sc.seed,      # step 3 starts here:
                           'hazard_types': list(sc.hazard_types), 'status': 'created',    # the run row is needed
                           'started_at': now, 'ended_at': None})                          # for the truth's id
truth = TruthState(run_id)
truth.set_scenario({'seed': sc.seed, 'hazard_types': list(sc.hazard_types), 'params': sc.params(),
                    'map_version_id': m.version, 'simulator_version': SIMULATOR_VERSION,
                    'started_at': now, 'ended_at': None})
compute_building_damage(m, sc, truth)
compute_road_damage(m, truth)
compute_people(m, sc, truth)
i = [d['intensity'] for d in truth.building_damage.values()]
people = collections.Counter()
for g in truth.people.values():
    people[g['condition']] += g['count']
print(f'   shaking: Intensity {shindo_class(min(i))} to {shindo_class(max(i))} (Japan scale)')
print(f'   buildings: {dict(collections.Counter(d["damage_level"] for d in truth.building_damage.values()))}')
print(f'   roads: {dict(collections.Counter(r["status"] for r in truth.road_status.values()))} (the rest OPEN)')
print(f'   people: {dict(people)}')

# --- 3. reports into Neon ops -------------------------------------------------------------------
reports = generate_reports(m, sc, truth)
print(f'3. run {run_id} created in ops.simulation_runs '
      f'(status {storage._call("GET", f"/runs/{run_id}")["status"]})')
print(f'   {len(reports)} reports to post: {dict(collections.Counter(r["observation"]["channel"] for r in reports))}')
t = time.time()
for n, item in enumerate(reports, 1):
    obs = item['observation']
    storage.save_observation(obs)                      # POST /runs/{id}/observations
    for link in item['links']:                         # answer key: stays local, never Neon
        truth.add_link(obs['id'], link['match_type'], link['truth_entity_type'], link['truth_entity_id'],
                       link['truth_simulation_time_s'])
    if n == 1:
        print(f'   after the first report the run is {storage._call("GET", f"/runs/{run_id}")["status"]}')
    if n % 50 == 0 or n == len(reports):
        print(f'   posted {n}/{len(reports)} ({time.time() - t:.0f} s)')
truth.flush(storage)                                   # truth rows -> simulation/runs/<run_id>/ only
print('   sample of what agents now see in ops.observations:')
for item in reports[:3]:
    o = item['observation']
    print(f'     t+{o["sim_time_s"]:>6}s  {o["channel"]:<18} [{o["location_precision"]}] {o["text_en"][:90]}')
print(f'4. check it: backend/.venv/bin/python scripts/check_sim_run.py {run_id}')
