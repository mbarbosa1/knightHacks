"""Download the drivable OSM road graph for the demo box and write the files the simulator and DB need.

Run on a laptop (the build sandbox can't reach OpenStreetMap):
    pip install osmnx
    python3 scripts/build_road_graph.py

Writes:
    data/road_graph.graphml        NetworkX MultiDiGraph (load with osmnx.load_graphml)
    data/road_segments.geojson     one LineString per graph edge, id = "<u>_<v>_<key>" (same as NetworkX)
    data/road_segments.meta.json   download date, osmnx version, counts
    data/facilities.geojson        access_node filled with the nearest routable graph node (updated in place)

Then run the repo's scripts/load_static.py --replace to load roads into Neon.
"""
import json, math, pathlib, datetime
import networkx as nx
import osmnx as ox
from shapely.geometry import LineString, mapping

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
NETWORK_TYPE = 'drive'   # emergency vehicles; change to 'drive_service' or 'all' for alleys


def download():
    b = json.load(open(DATA / 'bbox.json'))['road_bbox']
    ox.settings.useful_tags_way = list(dict.fromkeys(ox.settings.useful_tags_way + ['name:en']))
    try:      # osmnx >= 2.0
        G = ox.graph_from_bbox(bbox=(b['west'], b['south'], b['east'], b['north']),
                               network_type=NETWORK_TYPE, truncate_by_edge=True)
    except TypeError:  # osmnx 1.x
        G = ox.graph_from_bbox(b['north'], b['south'], b['east'], b['west'],
                               network_type=NETWORK_TYPE, truncate_by_edge=True)
    G = ox.add_edge_speeds(G, fallback=30)  # km/h where OSM has no maxspeed and no highway-type mean
    G = ox.add_edge_travel_times(G)
    return G


def text(v):
    """OSMnx merges tags of simplified edges into lists."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return ' / '.join(dict.fromkeys(map(str, v))) if isinstance(v, list) else str(v)


def edge_id(u, v, k):
    return f'{u}_{v}_{k}'


def segments(G):
    feats = []
    for u, v, k, d in G.edges(keys=True, data=True):
        geom = d.get('geometry') or LineString([(G.nodes[u]['x'], G.nodes[u]['y']),
                                               (G.nodes[v]['x'], G.nodes[v]['y'])])
        oneway = d.get('oneway')
        feats.append({'type': 'Feature', 'geometry': mapping(geom), 'properties': {
            'id': edge_id(u, v, k), 'from_node': int(u), 'to_node': int(v),
            'length_m': round(float(d['length']), 2),
            'speed_kmh': round(float(d['speed_kph']), 1) if d.get('speed_kph') is not None else None,
            'oneway': bool(oneway) if oneway is not None else None,
            'name_en': text(d.get('name:en')), 'name_ja': text(d.get('name')),
            'highway': text(d.get('highway')),
        }})
    return feats


def haversine_m(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def fill_access_nodes(G, facilities):
    """Nearest node in the largest strongly connected component, so every facility can be routed to and from."""
    core = max(nx.strongly_connected_components(G), key=len)
    nodes = [(n, G.nodes[n]['x'], G.nodes[n]['y']) for n in core]
    for f in facilities['features']:
        lon, lat = f['geometry']['coordinates']
        n, d = min(((n, haversine_m(lon, lat, x, y)) for n, x, y in nodes), key=lambda t: t[1])
        f['properties']['access_node'] = int(n)
        print(f"  {f['properties']['name_en']}: node {n} ({d:.0f} m)")
    return len(core)


def main():
    G = download()
    ox.save_graphml(G, DATA / 'road_graph.graphml')
    feats = segments(G)
    ids = [f['properties']['id'] for f in feats]
    assert len(ids) == len(set(ids)), 'duplicate edge ids'
    json.dump({'type': 'FeatureCollection', 'features': feats},
              open(DATA / 'road_segments.geojson', 'w'), ensure_ascii=False, separators=(',', ':'))

    fac = json.load(open(DATA / 'facilities.geojson'))
    print('facility access nodes:')
    core = fill_access_nodes(G, fac)
    json.dump(fac, open(DATA / 'facilities.geojson', 'w'), ensure_ascii=False, indent=1)

    meta = {'osm_version': f'OpenStreetMap via Overpass, downloaded {datetime.date.today().isoformat()}',
            'osmnx_version': ox.__version__, 'network_type': NETWORK_TYPE,
            'bbox': json.load(open(DATA / 'bbox.json'))['road_bbox'],
            'nodes': G.number_of_nodes(), 'edges': G.number_of_edges(), 'largest_scc_nodes': core,
            'edge_id_format': '<u>_<v>_<key>'}
    json.dump(meta, open(DATA / 'road_segments.meta.json', 'w'), indent=2)
    print(json.dumps(meta, indent=2))


if __name__ == '__main__':
    main()
