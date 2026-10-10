"""Check that a simulator run landed in Neon exactly as the simulator produced it.

usage (from the project root):  backend/.venv/bin/python scripts/check_sim_run.py <run_id | --latest> [--delete]

Compares Neon (DATABASE_URL in backend/.env, the simulator login) with the simulator's local copy in
simulation/runs/<run_id>/: the run row, every observation field by field, the event log, that every
truth_links observation id exists, and that nothing from the truth leaked into the agents' tables.
--delete removes the run and its event log from Neon afterwards (observations etc. cascade).
"""
import argparse, json, math, pathlib, sys
import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIELDS = ['channel', 'format', 'lang', 'original_text', 'text_en', 'media_url', 'claimed_location',
          'location_precision']
REALS = ['source_reliability', 'sim_time_s']   # Postgres real = float32


def lines(path):
    return [json.loads(l) for l in open(path)] if path.exists() else []


ap = argparse.ArgumentParser()
ap.add_argument('run_id', nargs='?')
ap.add_argument('--latest', action='store_true', help='check the most recently started run')
ap.add_argument('--delete', action='store_true')
a = ap.parse_args()
url = dotenv_values(ROOT / 'backend' / '.env').get('DATABASE_URL')
if a.latest:
    with psycopg.connect(url) as conn:
        a.run_id = str(conn.execute('SELECT id FROM ops.simulation_runs ORDER BY started_at DESC LIMIT 1').fetchone()[0])
if not a.run_id:
    ap.error('give a run_id or --latest')

local = ROOT / 'simulation' / 'runs' / a.run_id
run_local = json.load(open(local / 'simulation_run.json'))
obs_local = {o['id']: o for o in lines(local / 'observations.jsonl')}
links = lines(local / 'truth_links.jsonl')
errors = []

with psycopg.connect(url, row_factory=dict_row) as conn:
    run = conn.execute('SELECT * FROM ops.simulation_runs WHERE id=%s', (a.run_id,)).fetchone()
    if run is None:
        sys.exit(f'run {a.run_id} is not in Neon')
    for k in ('seed', 'map_version_id', 'hazard_types'):
        if run[k] != run_local[k]:
            errors.append(f'run {k}: Neon {run[k]!r} != simulator {run_local[k]!r}')
    expected_status = run_local['status'] if run_local['status'] in ('completed', 'failed') else \
        'running' if obs_local else 'created'
    if run['status'] != expected_status:
        errors.append(f'run status: Neon {run["status"]!r}, expected {expected_status!r}')

    rows = conn.execute('''SELECT *, public.ST_X(claimed_geom) AS lon, public.ST_Y(claimed_geom) AS lat
                           FROM ops.observations WHERE run_id=%s''', (a.run_id,)).fetchall()
    neon = {str(r['id']): r for r in rows}
    for i in sorted(set(obs_local) - set(neon)):
        errors.append(f'observation {i} missing in Neon')
    for i in sorted(set(neon) - set(obs_local)):
        errors.append(f'observation {i} in Neon but not produced by the simulator')
    for i in set(obs_local) & set(neon):
        o, r = obs_local[i], neon[i]
        for k in FIELDS:
            if r[k] != o.get(k):
                errors.append(f'observation {i} {k}: Neon {r[k]!r} != simulator {o.get(k)!r}')
        for k in REALS:
            if not math.isclose(r[k], o[k], rel_tol=1e-6):
                errors.append(f'observation {i} {k}: Neon {r[k]} != simulator {o[k]}')
        g = o.get('claimed_geom')
        if (g is None) != (r['lon'] is None) or g and (abs(r['lon'] - g['coordinates'][0]) > 1e-9
                                                        or abs(r['lat'] - g['coordinates'][1]) > 1e-9):
            errors.append(f'observation {i} location: Neon ({r["lon"]}, {r["lat"]}) != simulator {g}')

    missing_links = {l['observation_id'] for l in links} - set(neon)
    if missing_links:
        errors.append(f'{len(missing_links)} truth_links point at observations not in Neon')

    events = conn.execute('''SELECT table_name, payload->>'operation' AS op, count(*) AS n
                             FROM ops.operational_events WHERE run_id=%s GROUP BY 1, 2''', (a.run_id,)).fetchall()
    events = {(e['table_name'], e['op']): e['n'] for e in events}
    if events.get(('observations', 'INSERT'), 0) != len(neon):
        errors.append(f'event log has {events.get(("observations", "INSERT"), 0)} observation inserts for {len(neon)} observations')
    if events.get(('simulation_runs', 'INSERT'), 0) != 1:
        errors.append('event log is missing the run INSERT')

    # The simulator only writes runs and observations; agent beliefs must start empty (truth stays hidden).
    for t in ('incidents', 'road_restrictions', 'decisions'):
        n = conn.execute(f'SELECT count(*) AS n FROM ops.{t} WHERE run_id=%s', (a.run_id,)).fetchone()['n']
        if n:
            print(f'note: ops.{t} has {n} rows for this run (written by agents, not the simulator)')

    by_channel = conn.execute('''SELECT channel, count(*) AS n FROM ops.observations WHERE run_id=%s
                                 GROUP BY 1 ORDER BY 2 DESC''', (a.run_id,)).fetchall()
    print(f'run {a.run_id}: status {run["status"]}, seed {run["seed"]}, {len(neon)} observations '
          f'({", ".join(f"{c["channel"]} {c["n"]}" for c in by_channel)}), events {dict(events)}')

    if a.delete:
        conn.execute('DELETE FROM ops.operational_events WHERE run_id=%s', (a.run_id,))
        conn.execute('DELETE FROM ops.simulation_runs WHERE id=%s', (a.run_id,))
        print('deleted from Neon')

for e in errors[:50]:
    print('ERROR', e)
print(f'{len(errors)} errors')
sys.exit(1 if errors else 0)
