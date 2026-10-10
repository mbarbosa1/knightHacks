"""Check the data files against the static schema (database/final-database.md) without a database.

usage: python3 scripts/validate.py      exits 1 on any error; warnings don't fail
"""
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
sys.path.insert(0, str(ROOT / 'scripts'))
from load_static import BUILDING_COLS, FACILITY_COLS, ROAD_COLS  # noqa: E402

INT, REAL, TEXT, BOOL, JSON = int, (int, float), str, bool, dict
TYPES = {
    'buildings': {'height_m': REAL, 'floors': INT, 'basement_floors': INT, 'footprint_m2': REAL,
                  'year_built': INT, 'era_probs': JSON, 'soil_amp': REAL},
    'facilities': {'capacity': INT, 'access_node': INT},
    'road_segments': {'from_node': INT, 'to_node': INT, 'length_m': REAL, 'speed_kmh': REAL, 'oneway': BOOL},
}
ENUMS = {
    'buildings': {'structure': {'wood', 'rc', 'steel', 'light_steel'},
                  'era': {'pre1971', '1971_1981', 'post1981'}},
    'facilities': {'type': {'hospital', 'fire_station', 'police', 'shelter'},
                   'building_match': {'inside', 'nearest'}},
}
GEOM = {'buildings': 'Polygon', 'facilities': 'Point', 'road_segments': 'LineString'}
REQUIRED = {'buildings': {'id'}, 'facilities': {'id', 'type'}, 'road_segments': {'id', 'from_node', 'to_node'}}

errors, warnings = [], []


def check(table, path, cols):
    feats = json.load(open(path))['features']
    ids = set()
    for i, f in enumerate(feats):
        p, g = f['properties'], f['geometry']
        where = f'{table}[{i}] {p.get("id")}'
        if g is None or g['type'] != GEOM[table]:
            errors.append(f'{where}: geometry {g and g["type"]}, expected {GEOM[table]}')
        else:
            pts = g['coordinates'][0] if table == 'buildings' else g['coordinates'] if table == 'road_segments' else [g['coordinates']]
            if not all(122 < x < 154 and 20 < y < 46 for x, y in pts[:50]):
                errors.append(f'{where}: coordinates are not lon/lat in Japan')
        for c in cols:
            if c not in p:
                errors.append(f'{where}: missing column {c}'); continue
            v = p[c]
            if v is None:
                if c in REQUIRED[table]: errors.append(f'{where}: {c} is null')
                continue
            t = TYPES[table].get(c, TEXT)
            if not isinstance(v, t) or (t is INT and isinstance(v, bool)):
                errors.append(f'{where}: {c}={v!r} is not {t}')
            if c in ENUMS.get(table, {}) and v not in ENUMS[table][c]:
                errors.append(f'{where}: {c}={v!r} not in {sorted(ENUMS[table][c])}')
        extra = set(p) - set(cols)
        if extra and i == 0:
            warnings.append(f'{table}: properties not loaded into the DB: {sorted(extra)}')
        if p.get('id') in ids: errors.append(f'{where}: duplicate id')
        ids.add(p.get('id'))
        if table == 'buildings' and p.get('era_probs') and abs(sum(p['era_probs'].values()) - 1) > 1e-6:
            errors.append(f'{where}: era_probs do not sum to 1')
    print(f'{table}: {len(feats)} features checked')
    return feats, ids


b, bids = check('buildings', DATA / 'buildings.geojson', BUILDING_COLS)
fa, fids = check('facilities', DATA / 'facilities.geojson', FACILITY_COLS)

# cross references (the DB enforces these too)
for f in fa:
    p = f['properties']
    if p['building_id'] and p['building_id'] not in bids:
        errors.append(f'facility {p["id"]}: building_id {p["building_id"]} not in buildings')
for f in b:
    p = f['properties']
    if p['facility_id'] and p['facility_id'] not in fids:
        errors.append(f'building {p["id"]}: facility_id {p["facility_id"]} not in facilities')
n_empty = sum(f['properties']['access_node'] is None for f in fa)
if n_empty:
    warnings.append(f'facilities: access_node empty on {n_empty}/{len(fa)} (run scripts/build_road_graph.py)')
n_soil = sum(f['properties']['soil_amp'] is not None for f in b)
if n_soil < len(b):
    warnings.append(f'buildings: soil_amp set on {n_soil}/{len(b)} (load J-SHIS with scripts/load_jshis_soil.py)')

roads = DATA / 'road_segments.geojson'
if roads.exists():
    r, _ = check('road_segments', roads, ROAD_COLS)
    nodes = {n for f in r for n in (f['properties']['from_node'], f['properties']['to_node'])}
    for f in fa:
        an = f['properties']['access_node']
        if an is not None and an not in nodes:
            errors.append(f'facility {f["properties"]["id"]}: access_node {an} is not a road node')
    for f in r[:1]:
        p = f['properties']
        if p['id'] != f'{p["from_node"]}_{p["to_node"]}_{p["id"].rsplit("_", 1)[-1]}':
            errors.append(f'road {p["id"]}: id is not <from_node>_<to_node>_<key>')
else:
    warnings.append('road_segments.geojson missing (run scripts/build_road_graph.py on a laptop)')

for w in warnings: print('WARN ', w)
for e in errors[:50]: print('ERROR', e)
print(f'{len(errors)} errors, {len(warnings)} warnings')
sys.exit(1 if errors else 0)
