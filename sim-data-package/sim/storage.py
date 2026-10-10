"""Storage interface (docs/simulator-runtime.md) and its local-file implementation.

Simulation code only calls load_static_map / save_run / save_truth_batch / save_observation, so the
Neon + Databricks implementation can replace LocalStorage without touching it.
"""
import json, pathlib
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
        G = None
        if graph:
            import osmnx as ox
            G = ox.load_graphml(self.data_dir / 'road_graph.graphml')
            missing = {f'{u}_{v}_{k}' for u, v, k in G.edges(keys=True)} ^ {x['properties']['id'] for x in r}
            assert not missing, f'road graph and road_segments.geojson disagree on {len(missing)} ids'
        return StaticMap(
            version,
            {x['properties']['id']: x['properties'] for x in b},
            {x['properties']['id']: shape(x['geometry']) for x in b},
            {x['properties']['id']: {**x['properties'], 'lon': x['geometry']['coordinates'][0],
                                     'lat': x['geometry']['coordinates'][1]} for x in f},
            {x['properties']['id']: x['properties'] for x in r},
            {x['properties']['id']: shape(x['geometry']) for x in r},
            G)

    def _append(self, run_id, name, rows):
        d = self.runs_dir / str(run_id)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / name, 'a') as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')

    def save_run(self, run):
        """ops.simulation_runs row; rewritten on status change."""
        d = self.runs_dir / str(run['id'])
        d.mkdir(parents=True, exist_ok=True)
        json.dump(run, open(d / 'simulation_run.json', 'w'), indent=1)

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
