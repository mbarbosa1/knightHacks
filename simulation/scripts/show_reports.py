"""Print reports readably, one per block, in arrival order.

    python scripts/show_reports.py runs/reports.json                  # file from sim.run --save-reports (or Gerard's sample)
    python scripts/show_reports.py runs/<run_id>/observations.jsonl    # what the agents actually see
    options: --n 30  --group radio  --channel social_media  --lang ja  --kind rescue
Files with recipes also show kind and match_type (the answer key: simulator team only).
"""
import argparse, json, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sim.reports import CHANNEL_GROUP

sys.stdout.reconfigure(encoding='utf-8')   # Windows consoles default to cp1252 and crash on Japanese
ap = argparse.ArgumentParser()
ap.add_argument('path')
ap.add_argument('--n', type=int, default=20)
ap.add_argument('--channel')
ap.add_argument('--group', choices=sorted(set(CHANNEL_GROUP.values())))
ap.add_argument('--lang')
ap.add_argument('--kind')
a = ap.parse_args()

with open(a.path, encoding='utf-8') as f:
    if a.path.endswith('.jsonl'):
        items = [{'observation': json.loads(l)} for l in f if l.strip()]
    else:
        d = json.load(f)
        items = d['reports'] if isinstance(d, dict) else d

shown = 0
for it in sorted(items, key=lambda x: x['observation']['sim_time_s']):
    o, r = it['observation'], it.get('recipe') or {}
    group = CHANNEL_GROUP.get(o['channel'], o['channel'])
    if (a.group and group != a.group) or (a.channel and o['channel'] != a.channel) or (a.lang and o['lang'] != a.lang) or (a.kind and r.get('kind') != a.kind):
        continue
    m, s = divmod(int(o['sim_time_s']), 60)
    tag = f"  [{r['kind']} / {r['match_type']}]" if r else ''
    print(f"{m:2d}:{s:02d}  {group} ({o['channel']})  {o['lang']}  rel {o['source_reliability']}{tag}")
    print(f"       {o['original_text']}")
    if o['lang'] != 'en':
        print(f"       EN: {o['text_en']}")
    print()
    shown += 1
    if shown >= a.n:
        break
print(f'{shown} shown of {len(items)} reports')
