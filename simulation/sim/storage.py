"""Storage interface (docs/simulator-runtime.md) and its local-file implementation.

Simulation code only calls load_static_map / save_run / save_truth_batch / save_observation, so the
Neon + Databricks implementation can replace LocalStorage without touching it.
"""
import datetime, json, pathlib
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent


@dataclass
class StaticMap:
    version: str
    buildings: dict                 # id -> properties (static.buildings columns)
    building_geoms: dict            # id -> shapely Polygon, EPSG:4326
    facilities: dict                # id -> properties + lon, lat
    roads: dict                     # "<u>_<v>_<key>" -> properties (static.road_segments columns)
    road_geoms: dict                # id -> shapely LineString, EPSG:4326
    graph: object = field(repr=False, default=None)   # NetworkX MultiDiGraph with travel_time (s) per edge


ERAS = ('pre1971', '1971_1981', 'post1981')   # the order ages are rolled in, whatever order a source lists them
# Decimals each value has in the source files. Postgres REAL columns hand back float32 noise
# (1.3429 -> 1.34290003...), which is enough to tip a dice roll, so every source is rounded the same way.
DECIMALS = {'buildings': {'soil_amp': 4, 'height_m': 1, 'footprint_m2': 1}, 'roads': {'length_m': 2, 'speed_kmh': 1}}


def build_graph(roads, road_geoms):
    """The road graph sim/routing.py needs, built from the road segments themselves.

    A MultiDiGraph whose edge (u, v, key) is road id "u_v_key", nodes with x/y (lon/lat), and edges with
    length (m), travel_time (s) and geometry. travel_time is length / speed, the same formula OSMnx's
    add_edge_travel_times uses. Every source builds its graph here, so routes do not depend on the source.
    """
    import networkx as nx
    G = nx.MultiDiGraph()
    for rid, p in roads.items():
        line = road_geoms[rid]
        # An OSMnx edge geometry runs from node u to node v, so its ends give the node coordinates.
        G.add_node(p['from_node'], x=line.coords[0][0], y=line.coords[0][1])
        G.add_node(p['to_node'], x=line.coords[-1][0], y=line.coords[-1][1])
        G.add_edge(p['from_node'], p['to_node'], key=int(rid.rsplit('_', 1)[1]), length=p['length_m'],
                   travel_time=p['length_m'] / (p['speed_kmh'] / 3.6), geometry=line)
    return G


def make_static_map(version, buildings, building_geoms, facilities, roads, road_geoms, graph=True):
    """The StaticMap every storage returns. A seed must give the same earthquake whether the map came
    from local files or from Neon, so everything that could differ between sources is fixed here:
    entries are ordered by id, numbers are rounded to their source precision, and each building's
    era_probs is put in ERAS order (Postgres jsonb reorders the keys)."""
    def tidy(props, decimals):
        p = dict(props)
        for key, n in decimals.items():
            if p.get(key) is not None:
                p[key] = round(p[key], n)
        return p

    b = {}
    for i in sorted(buildings):
        p = tidy(buildings[i], DECIMALS['buildings'])
        if p.get('era_probs'):
            p['era_probs'] = {e: p['era_probs'][e] for e in ERAS if e in p['era_probs']}
        b[i] = p
    r = {i: tidy(roads[i], DECIMALS['roads']) for i in sorted(roads)}
    rg = {i: road_geoms[i] for i in r}
    return StaticMap(version, b, {i: building_geoms[i] for i in b}, {i: facilities[i] for i in sorted(facilities)},
                     r, rg, build_graph(r, rg) if graph else None)


class LocalStorage:
    """Reads the map from data/ and writes each run to runs/<run_id>/ as JSON lines."""

    def __init__(self, data_dir=ROOT / 'data', runs_dir=ROOT / 'runs'):
        self.data_dir, self.runs_dir = pathlib.Path(data_dir), pathlib.Path(runs_dir)

    def load_static_map(self, version='shinjuku-v1', graph=True):
        from shapely.geometry import shape

        def features(name):
            return json.load(open(self.data_dir / name))['features']

        b = features('buildings.geojson')
        f = features('facilities.geojson')
        r = features('road_segments.geojson')
        return make_static_map(
            version,
            {x['properties']['id']: x['properties'] for x in b},
            {x['properties']['id']: shape(x['geometry']) for x in b},
            {x['properties']['id']: {**x['properties'], 'lon': x['geometry']['coordinates'][0],
                                     'lat': x['geometry']['coordinates'][1]} for x in f},
            {x['properties']['id']: x['properties'] for x in r},
            {x['properties']['id']: shape(x['geometry']) for x in r},
            graph)

    def _append(self, run_id, name, rows):
        d = self.runs_dir / str(run_id)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / name, 'a') as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')

    def save_run(self, run):
        """ops.simulation_runs row; rewritten on status change. Returns the run id (ApiStorage gets it from Neon)."""
        d = self.runs_dir / str(run['id'])
        d.mkdir(parents=True, exist_ok=True)
        json.dump(run, open(d / 'simulation_run.json', 'w'), indent=1)
        return run['id']

    def end_run(self, run_id, status='completed', sim_time_s=None):
        run = json.load(open(self.runs_dir / str(run_id) / 'simulation_run.json'))
        run.update(status=status, ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        self.save_run(run)
        return run

    def save_truth_batch(self, rows):
        """Rows carry 'table' (Databricks table name); one file per table. Dedupes on event_id like Databricks."""
        by_file = {}
        for row in rows:
            row = dict(row)
            table = row.pop('table')
            by_file.setdefault((row.get('scenario_id') or row['id'], f'{table}.jsonl'), []).append(row)
        for (run_id, name), new in by_file.items():
            path = self.runs_dir / str(run_id) / name
            seen = {json.loads(l).get('event_id') for l in open(path)} if path.exists() else set()
            self._append(run_id, name, [x for x in new if x.get('event_id') is None or x['event_id'] not in seen])

    def save_observation(self, obs):
        self._append(obs['run_id'], 'observations.jsonl', [obs])
