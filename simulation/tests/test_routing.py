import pathlib, sys
import networkx as nx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sim.routing import Router, statuses, LIMITED_FACTOR
from sim.storage import LocalStorage


def toy():
    """A --10s--> B --10s--> C     plus a slower way round A -> D -> C (15 s each) and a one-way spur C -> E.
       Two parallel edges A->B: key 0 takes 10 s, key 1 takes 30 s."""
    G = nx.MultiDiGraph()
    for n, (x, y) in {'A': (0, 0), 'B': (0.001, 0), 'C': (0.002, 0), 'D': (0.001, 0.001), 'E': (0.003, 0)}.items():
        G.add_node(n, x=139.70 + x, y=35.69 + y)
    def road(u, v, t, both=True, key=None):
        G.add_edge(u, v, key=key, travel_time=float(t), length=float(t) * 8)
        if both:
            G.add_edge(v, u, key=key, travel_time=float(t), length=float(t) * 8)
    road('A', 'B', 10); road('B', 'C', 10); road('A', 'D', 15); road('D', 'C', 15)
    road('C', 'E', 5, both=False)
    G.add_edge('A', 'B', key=1, travel_time=30.0, length=240.0)
    return G


def test_fastest_route_and_fallback():
    r = Router(toy()).route('A', 'C')
    assert r['nodes'] == ['A', 'B', 'C'] and r['edge_ids'] == ['A_B_0', 'B_C_0']
    assert r['travel_time_s'] == 20 and r['length_m'] == 160
    assert r['geojson']['coordinates'][0] == [139.70, 35.69] and len(r['geojson']['coordinates']) == 3
    assert r['fallback']['nodes'] == ['A', 'D', 'C'] and r['fallback']['travel_time_s'] == 30


def test_blocked_road_is_avoided():
    r = Router(toy()).route('A', 'C', {'B_C_0': 'BLOCKED'})
    assert r['nodes'] == ['A', 'D', 'C'] and 'B_C_0' not in r['edge_ids']


def test_blocked_parallel_edge_falls_back_to_the_other():
    r = Router(toy()).route('A', 'B', {'A_B_0': 'BLOCKED'}, alternative=False)
    assert r['edge_ids'] == ['A_B_1'] and r['travel_time_s'] == 30


def test_limited_road_is_slower_but_usable():
    R = Router(toy())
    assert R.route('A', 'B', {'A_B_0': 'LIMITED'}, alternative=False)['travel_time_s'] == 10 * LIMITED_FACTOR
    # limiting both legs of the direct way (2 x 30 s) makes the way round (30 s) faster
    assert R.route('A', 'C', {'A_B_0': 'LIMITED', 'B_C_0': 'LIMITED'}, alternative=False)['nodes'] == ['A', 'D', 'C']


def test_unreachable_and_one_way():
    R = Router(toy())
    assert R.is_reachable('A', 'E') and not R.is_reachable('E', 'A')          # C -> E is one-way
    assert R.route('A', 'C', {'B_C_0': 'BLOCKED', 'D_C_0': 'BLOCKED'}) is None
    assert R.route('A', 'A')['travel_time_s'] == 0


def test_no_fallback_when_only_one_way_exists():
    assert Router(toy()).route('C', 'E')['fallback'] is None


def test_affected_edges():
    R = Router(toy())
    r = R.route('A', 'C')
    assert R.affected_edges(r['edge_ids'], {}) == []
    assert R.affected_edges(r['edge_ids'], {'B_C_0': 'BLOCKED', 'A_D_0': 'BLOCKED'}) == [('B_C_0', 'BLOCKED')]


def test_nearest_units():
    R = Router(toy())
    units = [{'id': 'amb1', 'type': 'ambulance', 'node': 'A'}, {'id': 'amb2', 'type': 'ambulance', 'node': 'D'},
             {'id': 'eng1', 'type': 'fire_engine', 'node': 'C'}, {'id': 'amb3', 'type': 'ambulance', 'node': 'B', 'status': 'busy'},
             {'id': 'amb4', 'type': 'ambulance', 'node': 'E'}]
    got = R.nearest_units('C', units, type='ambulance', k=5)
    assert [(u['unit_id'], u['travel_time_s']) for u in got] == [('amb2', 15), ('amb1', 20)]   # busy and stranded units left out
    assert R.nearest_units('C', units, k=1)[0]['unit_id'] == 'eng1'
    assert [u['unit_id'] for u in R.nearest_units('C', units, type='ambulance', status={'B_C_0': 'BLOCKED', 'C_B_0': 'BLOCKED'})] == ['amb2', 'amb1']


def test_facility_options_keeps_unknown_capacity():
    R = Router(toy())
    fac = {'h1': {'id': 'h1', 'type': 'hospital', 'name_en': 'Far', 'capacity': None, 'access_node': 'C'},
           'h2': {'id': 'h2', 'type': 'hospital', 'name_en': 'Near', 'capacity': 40, 'access_node': 'B'},
           's1': {'id': 's1', 'type': 'shelter', 'name_en': 'School', 'capacity': None, 'access_node': 'D'}}
    got = R.facility_options('A', fac, type='hospital')
    assert [(f['facility_id'], f['travel_time_s'], f['capacity']) for f in got] == [('h2', 10, 40), ('h1', 20, None)]


def test_assign_minimises_total_time_and_skips_impossible_pairs():
    R = Router(toy())
    units = [{'id': 'u1', 'node': 'A'}, {'id': 'u2', 'node': 'B'}, {'id': 'u3', 'node': 'E'}]
    incidents = [{'id': 'i1', 'node': 'B'}, {'id': 'i2', 'node': 'C'}]
    got = R.assign(units, incidents)
    # greedy would send u2 to i1 (0 s) and u1 to i2 (20 s) = 20; the alternative u1->i1, u2->i2 is also 20: either is optimal
    assert sum(p['travel_time_s'] for p in got['assignments']) == 20
    assert got['unassigned_units'] == ['u3'] and got['unassigned_incidents'] == []
    stuck = R.assign([{'id': 'u3', 'node': 'E'}], incidents)
    assert stuck['assignments'] == [] and stuck['unassigned_incidents'] == ['i1', 'i2']


@pytest.fixture(scope='module')
def city():
    m = LocalStorage().load_static_map()
    return m, Router(m.graph)


def test_real_graph_route_between_facilities(city):
    m, R = city
    fire = next(f for f in m.facilities.values() if f['type'] == 'fire_station')
    hosp = next(f for f in m.facilities.values() if f['type'] == 'hospital')
    r = R.route(fire['access_node'], hosp['access_node'])
    assert r and set(r['edge_ids']) <= set(m.roads) and r['fallback'] is not None
    assert r['length_m'] == pytest.approx(sum(m.roads[e]['length_m'] for e in r['edge_ids']), rel=1e-3)
    # A* must agree with plain Dijkstra on the best time
    best = nx.shortest_path_length(m.graph, fire['access_node'], hosp['access_node'], weight='travel_time')
    assert r['travel_time_s'] == pytest.approx(best, abs=0.1)
    assert r['fallback']['travel_time_s'] >= r['travel_time_s']


def test_real_graph_snaps_points_and_avoids_blocked_roads(city):
    m, R = city
    a, b = (139.6995, 35.6900), (139.7080, 35.6975)
    r = R.route(a, b, alternative=False)
    assert r is not None
    blocked = {e: 'BLOCKED' for e in r['edge_ids'][len(r['edge_ids']) // 2:][:1]}
    r2 = R.route(a, b, blocked, alternative=False)
    assert r2 is not None and not set(blocked) & set(r2['edge_ids']) and r2['travel_time_s'] >= r['travel_time_s']
    assert R.affected_edges(r['edge_ids'], blocked) == [(next(iter(blocked)), 'BLOCKED')]


def test_statuses_from_truth():
    assert statuses({'r1': {'status': 'BLOCKED', 'cause': 'debris', 'sim_time_s': 0}}) == {'r1': 'BLOCKED'}
