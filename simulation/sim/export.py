"""Write the map and one run's hidden truth as files the frontend can load.

    python -m sim.export --seed 42 [--mw 7.4] [--out ../frontend/public/sim]

A stand-in until the frontend can fetch from the backend. The map files have the same shape as the
backend's bulk endpoints (GET /map/versions/{id}/buildings | roads | facilities), so switching the
frontend to the API is a change of address. run.json stands in for GET /runs/{id}/truth.
Everything written here is hidden truth or derived from local data: it is gitignored.
"""
import argparse, collections, json, pathlib
from .damage_model import shindo_class
from .run import start_run
from .storage import ROOT

DEFAULT_OUT = ROOT.parent / 'frontend' / 'public' / 'sim'
BUILDING_PROPS = ('id', 'height_m', 'floors', 'usage', 'structure', 'name_en', 'name_ja', 'address_en',
                  'facility_id', 'facility_type', 'facility_name_en')
ROAD_PROPS = ('id', 'length_m', 'speed_kmh', 'oneway', 'name_en', 'name_ja')
FACILITY_PROPS = ('id', 'type', 'subtype', 'name_en', 'name_ja', 'building_id', 'capacity')


def _ring(coords):
    return [[round(x, 6), round(y, 6)] for x, y in coords]


def _dump(obj, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(',', ':')))
    return path.stat().st_size


def export_map(m, out):
    """Static layers. Same Feature shape as the backend: top-level id, geometry, properties (with id)."""
    buildings = [{'type': 'Feature', 'id': b,
                  'geometry': {'type': 'Polygon', 'coordinates': [_ring(g.exterior.coords)] + [_ring(i.coords) for i in g.interiors]},
                  'properties': {k: m.buildings[b].get(k) for k in BUILDING_PROPS}}
                 for b, g in m.building_geoms.items()]
    roads = [{'type': 'Feature', 'id': r, 'geometry': {'type': 'LineString', 'coordinates': _ring(g.coords)},
              # node ids as strings, like the API, so JavaScript keeps their precision
              'properties': {**{k: m.roads[r].get(k) for k in ROAD_PROPS},
                             'from_node': str(m.roads[r]['from_node']), 'to_node': str(m.roads[r]['to_node'])}}
             for r, g in m.road_geoms.items()]
    facilities = [{**{k: f.get(k) for k in FACILITY_PROPS},
                   'access_node': str(f['access_node']) if f.get('access_node') is not None else None,
                   'geometry': {'type': 'Point', 'coordinates': [f['lon'], f['lat']]}} for f in m.facilities.values()]
    return {'buildings': _dump({'type': 'FeatureCollection', 'features': buildings}, out / 'map' / 'buildings.geojson'),
            'roads': _dump({'type': 'FeatureCollection', 'features': roads}, out / 'map' / 'roads.geojson'),
            'facilities': _dump(facilities, out / 'map' / 'facilities.json')}


def run_truth(truth, sc, m):
    """The part of TruthState a map needs. building_damage and road_status keep TruthState's shape."""
    hurt = collections.defaultdict(dict)
    totals = collections.Counter()
    for g in truth.people.values():
        totals[g['condition']] += g['count']
        if g['condition'] != 'fine':
            hurt[g['building_id']][g['condition']] = g['count']
    i = [d['intensity'] for d in truth.building_damage.values()]
    lo, hi = shindo_class(min(i)), shindo_class(max(i))
    return {'run_id': truth.run_id, 'map_version_id': m.version, 'seed': sc.seed, 'scenario': sc.params(),
            'intensity': {'min': min(i), 'max': max(i), 'label': lo if lo == hi else f'{lo} to {hi}'},
            'building_damage': {b: {'damage_level': d['damage_level'], 'intensity': d['intensity']}
                                for b, d in truth.building_damage.items() if d['damage_level'] != 'none'},
            'road_status': {r: {'status': s['status'], 'cause': s['cause']} for r, s in truth.road_status.items()},
            'people_by_building': hurt,
            'summary': {'buildings': dict(collections.Counter(d['damage_level'] for d in truth.building_damage.values())),
                        'roads': dict(collections.Counter(s['status'] for s in truth.road_status.values())),
                        'people': dict(totals), 'reports': len({l['observation_id'] for l in truth.truth_links})}}


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--seed', type=int, required=True)
    ap.add_argument('--mw', type=float, help='fix the magnitude instead of drawing it from the seed')
    ap.add_argument('--out', type=pathlib.Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    truth, sc, m = start_run(a.seed, mw=a.mw)
    sizes = export_map(m, a.out)
    data = run_truth(truth, sc, m)
    sizes['run'] = _dump(data, a.out / 'run.json')
    print(f'wrote {a.out}:', {k: f'{v / 1e6:.2f} MB' for k, v in sizes.items()})
    print(f"seed {sc.seed}  M{sc.mw}  Intensity {data['intensity']['label']} (Japan scale)  {data['summary']}")
