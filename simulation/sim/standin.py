"""A stand-in dispatcher: sends units to reported incidents so the map has something to move.

NOT the agents and not the live loop. It exists so the frontend can be designed before those do.
It cheats in one way: it uses the answer key to know which building a report is about. Everything
else is honest: it plans on what is known at the time, units drive through the true roads, and a unit
that meets a blocked road stops, reports it, and is rerouted.
"""
import collections
from .routing import LIMITED_FACTOR, statuses

UNITS_PER_FACILITY = {'fire_station': ('Engine', 2), 'hospital': ('Ambulance', 2), 'police': ('Patrol', 1)}
APPROVAL_DELAY_S = 30      # from "incident believed" to the unit leaving
SINGLE_REPORT_WAIT_S = 120  # an incident with one report is acted on after this long anyway
REROUTE_PAUSE_S = 20
MAX_REROUTES = 4


def _twins(road_id, truth_status):
    """A blocked street is blocked both ways: return the id and its reverse if that is blocked too."""
    u, v, k = road_id.rsplit('_', 2)
    return [r for r in (road_id, f'{v}_{u}_{k}') if truth_status.get(r) == 'BLOCKED']


def standin_dispatch(static_map, truth, router, reports):
    """-> one entry per dispatched unit: where it is at every moment, and what happened to it."""
    m, G = static_map, router.G
    truth_status = statuses(truth.road_status)
    need = collections.Counter()
    for g in truth.people.values():
        if g['condition'] in ('trapped', 'critical'):
            need[g['building_id']] += g['count']
    seen = collections.defaultdict(list)   # building -> times it was reported as a rescue
    for item in reports:
        bid = next((l['truth_entity_id'] for l in item['links'] if l['truth_entity_type'] == 'building'), None)
        if bid in need and item['recipe']['kind'] == 'rescue':
            seen[bid].append(item['observation']['sim_time_s'])
    ready = sorted((t[1] if len(t) > 1 else t[0] + SINGLE_REPORT_WAIT_S, b) for b, t in seen.items())

    units = []
    for f in m.facilities.values():
        label, n = UNITS_PER_FACILITY.get(f['type'], (None, 0))
        for _ in range(n):
            units.append({'type': label, 'home': f['name_en'], 'node': f['access_node'], 'free': True})
    for label in {u['type'] for u in units}:
        for i, u in enumerate([u for u in units if u['type'] == label], 1):
            u['id'] = f'{label} {i}'

    known = {}   # road_id -> time a unit found it blocked (what the dispatcher has learned so far)
    out = []
    for believed_at, bid in ready:
        depart = believed_at + APPROVAL_DELAY_S
        belief = {r: 'BLOCKED' for r, t in known.items() if t <= depart}
        c = m.building_geoms[bid].centroid
        dest = router.node((c.x, c.y))
        to_dest = router.times_to(dest, belief)
        free = [u for u in units if u['free'] and u['node'] in to_dest]
        if not free:
            continue
        unit = min(free, key=lambda u: to_dest[u['node']])
        unit['free'] = False
        t, here, path, events, planned = depart, unit['node'], [], [], None
        events.append({'t': round(depart, 1), 'kind': 'dispatched', 'lon': G.nodes[here]['x'], 'lat': G.nodes[here]['y'],
                       'text': f"{unit['id']} sent from {unit['home']}"})
        for attempt in range(MAX_REROUTES + 1):
            route = router.route(here, dest, belief, alternative=False)
            if route is None:
                events.append({'t': round(t, 1), 'kind': 'stuck', 'lon': G.nodes[here]['x'], 'lat': G.nodes[here]['y'],
                               'text': f"{unit['id']} cannot reach the incident: every way in is blocked"})
                break
            if planned is None:
                planned = route['geojson']['coordinates']
            blocked_at = None
            for road_id, (u, v) in zip(route['edge_ids'], zip(route['nodes'], route['nodes'][1:])):
                status = truth_status.get(road_id, 'OPEN')
                if status == 'BLOCKED':
                    blocked_at = road_id
                    break
                k = road_id.rsplit('_', 1)[1]
                a = G[u][v][int(k)]
                secs = a['travel_time'] * (LIMITED_FACTOR if status == 'LIMITED' else 1)
                line = list(a['geometry'].coords) if a.get('geometry') is not None else \
                    [(G.nodes[u]['x'], G.nodes[u]['y']), (G.nodes[v]['x'], G.nodes[v]['y'])]
                for i, (x, y) in enumerate(line):
                    path.append([round(x, 6), round(y, 6), round(t + secs * i / (len(line) - 1), 1)])
                t, here = t + secs, v
            if blocked_at is None:
                events.append({'t': round(t, 1), 'kind': 'arrived', 'lon': c.x, 'lat': c.y,
                               'text': f"{unit['id']} on scene: {need[bid]} people to reach"})
                break
            found = _twins(blocked_at, truth_status)
            for r in found:
                known.setdefault(r, t)
                belief[r] = 'BLOCKED'
            name = m.roads[blocked_at].get('name_en') or 'a side street'
            events.append({'t': round(t, 1), 'kind': 'blocked', 'lon': G.nodes[here]['x'], 'lat': G.nodes[here]['y'],
                           'road_ids': found, 'text': f"{unit['id']} found {name} blocked by debris. Plan invalidated, rerouting."})
            if not path:
                path.append([G.nodes[here]['x'], G.nodes[here]['y'], round(t, 1)])
            t += REROUTE_PAUSE_S
            path.append([path[-1][0], path[-1][1], round(t, 1)])
        if not path:
            path = [[G.nodes[here]['x'], G.nodes[here]['y'], round(depart, 1)]]
        out.append({'id': unit['id'], 'type': unit['type'], 'home': unit['home'], 'building_id': bid,
                    'incident': {'lon': c.x, 'lat': c.y}, 'people': need[bid], 'depart_s': round(depart, 1),
                    'arrive_s': round(t, 1) if events[-1]['kind'] == 'arrived' else None,
                    'rerouted': any(e['kind'] == 'blocked' for e in events),
                    'planned': planned, 'path': path, 'events': events})
    return out
