"""Routing on the road graph: the tool functions agents call, and the simulator's own path planning.

Everything here is pathfinding over a status map {road_id: 'OPEN' | 'LIMITED' | 'BLOCKED'} (absent = OPEN).
Pass the agents' beliefs (ops.road_restrictions) to plan; the simulator checks truth when a unit moves.
Places are a graph node id or a (lon, lat) pair, which snaps to the nearest well-connected node.
"""
import math
import networkx as nx
import numpy as np
from scipy.optimize import linear_sum_assignment

LIMITED_FACTOR = 3.0      # travel time multiplier on a LIMITED road
ALT_PENALTY = 5.0         # multiplier on the main route's edges when looking for a fallback
INFEASIBLE = 1e9


def statuses(road_status):
    """TruthState.road_status ({id: {status, cause, ...}}) -> plain {id: status}."""
    return {r: s['status'] for r, s in road_status.items()}


class Router:
    def __init__(self, graph):
        self.G = graph
        self.Grev = graph.reverse(copy=False)
        # Snap targets: the largest strongly connected component, so a snapped place can be reached and left.
        core = max(nx.strongly_connected_components(graph), key=len)
        self._nodes = np.array(sorted(core))
        self._xy = np.array([[graph.nodes[n]['x'], graph.nodes[n]['y']] for n in self._nodes])
        self._kx = math.cos(math.radians(float(self._xy[:, 1].mean())))
        self._vmax = max(d['length'] / d['travel_time'] for _, _, d in graph.edges(data=True) if d['travel_time'] > 0)

    # --- places -----------------------------------------------------------------------------------
    def node(self, place):
        if isinstance(place, (tuple, list)):
            d = np.hypot((self._xy[:, 0] - place[0]) * self._kx, self._xy[:, 1] - place[1])
            return self._nodes[int(d.argmin())].item()   # a plain Python value, whatever type the node ids are
        return place

    def _metres(self, a, b):
        na, nb = self.G.nodes[a], self.G.nodes[b]
        return 111320 * math.hypot((na['x'] - nb['x']) * self._kx, na['y'] - nb['y'])

    # --- weights ----------------------------------------------------------------------------------
    def _edge_time(self, u, v, k, attrs, status, penalised=()):
        eid = f'{u}_{v}_{k}'
        s = status.get(eid, 'OPEN')
        if s == 'BLOCKED':
            return None
        t = attrs['travel_time'] * (LIMITED_FACTOR if s == 'LIMITED' else 1.0)
        return t * ALT_PENALTY if eid in penalised else t

    def _weight(self, status, penalised=(), reverse=False):
        """NetworkX weight function. On a MultiDiGraph d is {key: attrs}; the fastest usable parallel edge wins."""
        def w(u, v, d):
            if reverse:
                u, v = v, u
            times = [t for k, a in d.items() if (t := self._edge_time(u, v, k, a, status, penalised)) is not None]
            return min(times) if times else None
        return w

    # --- routes -----------------------------------------------------------------------------------
    def _describe(self, nodes, status):
        """Edge ids, real travel time (no fallback penalty), length and GeoJSON line for a node path."""
        edges, secs, metres, coords = [], 0.0, 0.0, []
        for u, v in zip(nodes, nodes[1:]):
            k, t = min(((k, t) for k, a in self.G[u][v].items()
                        if (t := self._edge_time(u, v, k, a, status)) is not None), key=lambda kt: kt[1])
            a = self.G[u][v][k]
            edges.append(f'{u}_{v}_{k}')
            secs += t
            metres += a['length']
            line = list(a['geometry'].coords) if a.get('geometry') is not None else \
                [(self.G.nodes[u]['x'], self.G.nodes[u]['y']), (self.G.nodes[v]['x'], self.G.nodes[v]['y'])]
            coords += [list(c) for c in (line if not coords else line[1:])]
        if not coords:   # start == destination
            n = self.G.nodes[nodes[0]]
            coords = [[n['x'], n['y']]]
        return {'nodes': list(nodes), 'edge_ids': edges, 'travel_time_s': round(secs, 1), 'length_m': round(metres, 1),
                'geojson': {'type': 'LineString', 'coordinates': coords}}

    def route(self, origin, destination, status=None, alternative=True):
        """Fastest route avoiding BLOCKED roads, or None if there is none.
        With alternative=True the result also has 'fallback': a second route that avoids the first where it can
        (None if no different route exists)."""
        status = status or {}
        s, t = self.node(origin), self.node(destination)
        heuristic = lambda a, b: self._metres(a, b) / self._vmax   # never overestimates: nothing is faster than vmax
        try:
            main = self._describe(nx.astar_path(self.G, s, t, heuristic=heuristic, weight=self._weight(status)), status)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        if alternative:
            alt = self._describe(nx.astar_path(self.G, s, t, heuristic=heuristic,
                                               weight=self._weight(status, penalised=set(main['edge_ids']))), status)
            main['fallback'] = alt if alt['edge_ids'] != main['edge_ids'] else None
        return main

    def is_reachable(self, origin, destination, status=None):
        return self.route(origin, destination, status, alternative=False) is not None

    def affected_edges(self, edge_ids, status):
        """Edges of an existing route that are no longer OPEN -> the plan needs replacing if this is non-empty."""
        return [(e, status[e]) for e in edge_ids if status.get(e, 'OPEN') != 'OPEN']

    # --- many-to-one / one-to-many ----------------------------------------------------------------
    def times_to(self, destination, status=None):
        """{node: seconds to drive from that node to destination} for every node that can reach it."""
        return nx.single_source_dijkstra_path_length(self.Grev, self.node(destination),
                                                     weight=self._weight(status or {}, reverse=True))

    def times_from(self, origin, status=None):
        return nx.single_source_dijkstra_path_length(self.G, self.node(origin), weight=self._weight(status or {}))

    def nearest_units(self, incident, units, type=None, k=3, status=None):
        """The k available units of a type that can reach the incident soonest.
        units: [{'id', 'type', 'status', 'node' or 'lon'/'lat'}]. Units that cannot get there are left out."""
        to_incident = self.times_to(incident, status)
        out = []
        for u in units:
            if u.get('status', 'available') != 'available' or (type and u.get('type') != type):
                continue
            n = self.node(u['node'] if u.get('node') is not None else (u['lon'], u['lat']))
            if n in to_incident:
                out.append({'unit_id': u['id'], 'type': u.get('type'), 'travel_time_s': round(to_incident[n], 1)})
        return sorted(out, key=lambda x: x['travel_time_s'])[:k]

    def facility_options(self, location, facilities, type=None, status=None):
        """Facilities of a type ranked by drive time from location. capacity None means unknown, not zero."""
        from_here = self.times_from(location, status)
        out = []
        for f in facilities.values() if isinstance(facilities, dict) else facilities:
            if type and f['type'] != type:
                continue
            n = f['access_node'] if f.get('access_node') is not None else self.node((f['lon'], f['lat']))
            if n in from_here:
                out.append({'facility_id': f['id'], 'type': f['type'], 'name_en': f.get('name_en'),
                            'capacity': f.get('capacity'), 'travel_time_s': round(from_here[n], 1)})
        return sorted(out, key=lambda x: x['travel_time_s'])

    def assign(self, units, incidents, status=None):
        """One unit per incident, minimising total drive time (Hungarian method).
        incidents: [{'id', 'node' or 'lon'/'lat'}]. Pairs with no route are never assigned; leftovers are returned."""
        units = [u for u in units if u.get('status', 'available') == 'available']
        place = lambda x: self.node(x['node'] if x.get('node') is not None else (x['lon'], x['lat']))
        cost = np.full((len(units), len(incidents)), INFEASIBLE)
        for j, inc in enumerate(incidents):
            to_inc = self.times_to(place(inc), status)
            for i, u in enumerate(units):
                cost[i, j] = to_inc.get(place(u), INFEASIBLE)
        pairs = []
        if cost.size:
            for i, j in zip(*linear_sum_assignment(cost)):
                if cost[i, j] < INFEASIBLE:
                    pairs.append({'unit_id': units[i]['id'], 'incident_id': incidents[j]['id'],
                                  'travel_time_s': round(float(cost[i, j]), 1)})
        done_u, done_i = {p['unit_id'] for p in pairs}, {p['incident_id'] for p in pairs}
        return {'assignments': pairs,
                'unassigned_units': [u['id'] for u in units if u['id'] not in done_u],
                'unassigned_incidents': [x['id'] for x in incidents if x['id'] not in done_i]}
