"""Fill buildings.soil_amp from the J-SHIS 250 m surface-soil amplification mesh.

Download on a laptop (blocked from the build sandbox): J-SHIS Map > Download > "Surface ground"
(表層地盤), 250 m mesh, map area 5339, and unzip the CSV into data/jshis/.

usage: python3 scripts/load_jshis_soil.py data/jshis/<file>.csv [--code-col CODE] [--arv-col ARV]

Writes soil_amp (J-SHIS ARV, relative to Vs=400 m/s) into data/buildings.geojson in place, using the
250 m mesh cell of each building's centroid. Then reload: python3 scripts/load_static.py --replace
Use in the simulator as PGV_surface = PGV600 * 1.31 * soil_amp (docs/REFERENCE.md section 2).
Not yet run against a real J-SHIS file: check the printed column names and match counts.
"""
import argparse, csv, json, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILDINGS = ROOT / 'data' / 'buildings.geojson'


def mesh250(lon, lat):
    """10-digit JIS X 0410 quarter mesh (250 m) code."""
    y, x = lat * 1.5, lon - 100
    p, u = int(y), int(x); y, x = (y - p) * 8, (x - u) * 8
    q, v = int(y), int(x); y, x = (y - q) * 10, (x - v) * 10
    r, w = int(y), int(x); y, x = (y - r) * 2, (x - w) * 2
    s, t = int(y), int(x); y, x = (y - s) * 2, (x - t) * 2
    return f'{p:02d}{u:02d}{q}{v}{r}{w}{s * 2 + t + 1}{int(y) * 2 + int(x) + 1}'


def centroid(geom):
    ring = geom['coordinates'][0][:-1]
    return sum(c[0] for c in ring) / len(ring), sum(c[1] for c in ring) / len(ring)


def read_mesh(path, code_col, arv_col):
    raw = pathlib.Path(path).read_bytes()
    for enc in ('utf-8', 'shift_jis', 'cp932'):
        try: text = raw.decode(enc); break
        except UnicodeDecodeError: continue
    header, rows = None, []
    for line in text.splitlines():
        if not line.strip(): continue
        if line.startswith('#'):
            header = [h.strip() for h in line.lstrip('#').split(',')]
            continue
        rows.append([c.strip() for c in next(csv.reader([line]))])
    if header is None and rows and not rows[0][0].isdigit():
        header = rows.pop(0)
    print('columns:', header)
    ci, ai = header.index(code_col), header.index(arv_col)
    return {r[ci]: float(r[ai]) for r in rows if r[ai] not in ('', '-')}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('csv')
    ap.add_argument('--code-col', default='CODE')
    ap.add_argument('--arv-col', default='ARV')
    a = ap.parse_args()
    arv = read_mesh(a.csv, a.code_col, a.arv_col)
    d = json.load(open(BUILDINGS))
    hit = 0
    for f in d['features']:
        v = arv.get(mesh250(*centroid(f['geometry'])))
        f['properties']['soil_amp'] = v
        hit += v is not None
    json.dump(d, open(BUILDINGS, 'w'), ensure_ascii=False, separators=(',', ':'))
    vals = [f['properties']['soil_amp'] for f in d['features'] if f['properties']['soil_amp'] is not None]
    print(f'{len(arv)} mesh cells read; soil_amp set on {hit}/{len(d["features"])} buildings'
          + (f'; ARV range {min(vals):.2f}-{max(vals):.2f}' if vals else ''))


if __name__ == '__main__':
    main()
