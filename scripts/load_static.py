"""Load the static map (buildings, facilities, roads if present) into Neon.

usage (from the project root):  backend/.venv/bin/python scripts/load_static.py [--map-version shinjuku-v1] [--replace]

Connects as map_import using MAP_IMPORT_DATABASE_URL from backend/.env or the environment.

Reads data/buildings.geojson, data/facilities.geojson and, when it exists, data/road_segments.geojson.
Everything goes in one transaction; --replace deletes that map version first so you can reload.
Run database/schema.sql once before this.
"""
import argparse, json, os, pathlib, sys
import psycopg
from dotenv import dotenv_values
from psycopg.types.json import Jsonb

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'

BUILDING_COLS = [
    'id', 'height_m', 'height_source', 'floors', 'floors_source', 'basement_floors', 'footprint_m2',
    'structure', 'structure_source', 'structure_basis', 'fire_resistance', 'fireproof_code',
    'year_built', 'year_source', 'era', 'era_probs', 'usage', 'usage_code', 'usage_source',
    'zoning', 'zoning_ja', 'soil_amp', 'name_en', 'name_ja', 'address_en', 'address_ja',
    'facility_id', 'facility_type', 'facility_subtype', 'facility_name_en', 'map_id', 'plateau_building_id']
FACILITY_COLS = ['id', 'type', 'name_en', 'name_ja', 'building_id', 'capacity', 'access_node',
                 'subtype', 'building_match', 'source', 'note']
ROAD_COLS = ['id', 'from_node', 'to_node', 'length_m', 'speed_kmh', 'oneway', 'name_en', 'name_ja']
JSON_COLS = {'era_probs'}

SOURCE_VERSIONS = {
    'plateau': 'PLATEAU 2023 Shinjuku/Shibuya (Flateau GeoParquet) + PLATEAU 2020 CityGML ids',
    'facilities': 'Overture Maps places 2026-09-23.1, hand-checked',
    'osm': None,      # filled from data/road_segments.meta.json when roads are loaded
    'jshis': None,    # filled when soil_amp is loaded (scripts/load_jshis_soil.py)
}


def features(path):
    return json.load(open(path))['features']


def insert(cur, table, cols, feats, mv):
    sql = (f'INSERT INTO static.{table} (map_version_id, geom, {", ".join(cols)}) '
           f'VALUES (%s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), {", ".join(["%s"] * len(cols))})')
    rows = []
    for f in feats:
        p = f['properties']
        vals = [Jsonb(p.get(c)) if c in JSON_COLS and p.get(c) is not None else p.get(c) for c in cols]
        rows.append([mv, json.dumps(f['geometry'])] + vals)
    cur.executemany(sql, rows)
    print(f'static.{table}: {len(rows)} rows')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--map-version', default='shinjuku-v1')
    ap.add_argument('--replace', action='store_true', help='delete this map version first')
    ap.add_argument('--notes', default='Shinjuku demo box, 1.5 km around Shinjuku Station')
    a = ap.parse_args()
    url = (os.environ.get('MAP_IMPORT_DATABASE_URL')
           or dotenv_values(ROOT / 'backend' / '.env').get('MAP_IMPORT_DATABASE_URL')
           or sys.exit('MAP_IMPORT_DATABASE_URL is not set in backend/.env (use the map_import login)'))

    roads = DATA / 'road_segments.geojson'
    sv = dict(SOURCE_VERSIONS)
    if roads.exists() and (DATA / 'road_segments.meta.json').exists():
        sv['osm'] = json.load(open(DATA / 'road_segments.meta.json')).get('osm_version')
    buildings = features(DATA / 'buildings.geojson')
    if any(f['properties'].get('soil_amp') is not None for f in buildings):
        sv['jshis'] = 'J-SHIS shallow soil amplification, 250 m mesh'

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        if a.replace:
            cur.execute('DELETE FROM static.map_versions WHERE id = %s', [a.map_version])
        cur.execute('INSERT INTO static.map_versions (id, source_versions, notes) VALUES (%s, %s, %s)',
                    [a.map_version, Jsonb(sv), a.notes])
        insert(cur, 'buildings', BUILDING_COLS, buildings, a.map_version)
        insert(cur, 'facilities', FACILITY_COLS, features(DATA / 'facilities.geojson'), a.map_version)
        if roads.exists():
            insert(cur, 'road_segments', ROAD_COLS, features(roads), a.map_version)
        else:
            print('static.road_segments: skipped (data/road_segments.geojson not built yet)')
    print(f'loaded map version {a.map_version}')  # commit happens on leaving the with-block; deferred FKs check here


if __name__ == '__main__':
    main()
