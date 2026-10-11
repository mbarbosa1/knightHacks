import json, pathlib, sys, uuid
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from shapely.geometry import Point
from sim.routing import Router
from sim.units import DispatchError, Fleet, REROUTE_PAUSE_S
from tests.test_routing import toy

RUN = '00000000-0000-4000-8000-000000000001'


def fleet(blocked=(), limited=()):
    """Toy map: A -10s-> B -10s-> C direct, A -15s-> D -15s-> C the long way. One engine parked at A."""
    G = toy()
    status = {**{r: {'status': 'BLOCKED', 'sim_time_s': 0.0} for r in blocked}, **{r: {'status': 'LIMITED', 'sim_time_s': 0.0} for r in limited}}
    links, reports, beliefs_written = [], [], []
    truth = SimpleNamespace(road_status=status, add_link=lambda *a: links.append(a))
    c = G.nodes['C']
    m = SimpleNamespace(facilities={'f1': {'access_node': 'A'}}, roads={'B_C_0': {'name_en': 'Yasukuni Street'}},
                        building_geoms={'bldg_x': Point(c['x'], c['y'])})
    a = G.nodes['A']
    resources = [{'id': str(uuid.uuid4()), 'type': 'fire_engine', 'name': 'Engine 1', 'home_facility_id': 'f1',
                  'location': {'longitude': a['x'], 'latitude': a['y']}}]
    f = Fleet(RUN, m, Router(G), truth, resources, emit=reports.append, on_blocked=lambda *x: beliefs_written.append(x))
    return f, next(iter(f.units.values())), G, reports, links, beliefs_written


def test_a_unit_drives_its_route_at_road_speed_and_arrives():
    f, unit, G, *_ = fleet()
    d = f.dispatch(unit.id, now_s=100, building_id='bldg_x')
    assert unit.status == 'en_route' and d['version'] == 1 and d['assignments'] == [{'resource_id': unit.id}]
    assert d['route_geojson']['properties']['road_ids'] == ['A_B_0', 'B_C_0'] and d['eta_s'] == 120
    f.tick(105)                                          # halfway along A -> B
    assert unit.lon == pytest.approx((G.nodes['A']['x'] + G.nodes['B']['x']) / 2) and unit.node == 'A'
    f.tick(112)
    assert unit.node == 'B' and unit.status == 'en_route'
    f.tick(500)
    assert unit.status == 'busy' and unit.node == 'C' and unit.task['arrived_s'] == 120
    assert (unit.lon, unit.lat) == (G.nodes['C']['x'], G.nodes['C']['y'])
    assert [e['kind'] for e in f.log] == ['dispatched', 'arrived']
    assert f.view()[0]['trail'][0] == [G.nodes['A']['x'], G.nodes['A']['y']] and f.view()[0]['route'] is None


def test_a_limited_road_is_driven_slowly():
    f, unit, *_ = fleet(limited=['A_B_0'])
    f.dispatch(unit.id, now_s=0, building_id='bldg_x')
    f.tick(25)
    assert unit.node == 'A'                              # 10 s road now takes 30 s
    f.tick(1000)
    assert unit.task['arrived_s'] == 40


def test_a_blocked_road_is_found_on_arrival_reported_and_routed_around():
    f, unit, G, reports, links, beliefs = fleet(blocked=['B_C_0', 'C_B_0'])
    decisions = []
    f._on_decision = decisions.append
    f.dispatch(unit.id, now_s=0, building_id='bldg_x')   # planned on beliefs: nobody knows it is blocked
    f.tick(9)
    assert reports == [] and unit.task['version'] == 1
    f.tick(11)                                           # reaches B at 10 s and finds the road ahead blocked
    assert unit.node == 'B' and unit.task['version'] == 2 and 'Yasukuni Street' in unit.task['reason']
    report = reports[0]
    assert report['channel'] == 'responder_update' and report['format'] == 'structured' and report['sim_time_s'] == 10
    assert json.loads(report['original_text']) == {'unit_id': unit.id, 'road_id': 'B_C_0', 'road_ids': ['B_C_0', 'C_B_0'], 'status': 'BLOCKED'}
    assert 'Yasukuni Street' in report['text_en'] and report['claimed_geom']['coordinates'] == [round(G.nodes['B']['x'], 6), round(G.nodes['B']['y'], 6)]
    assert beliefs == [(['B_C_0', 'C_B_0'], report['id'], 10)] and {l[3] for l in links} == {'B_C_0', 'C_B_0'}
    assert f.believed() == {'B_C_0': 'BLOCKED', 'C_B_0': 'BLOCKED'}
    f.tick(10 + REROUTE_PAUSE_S - 1)
    assert unit.node == 'B'                              # standing still while the new route is worked out
    f.tick(10_000)                                       # back B -> A -> D -> C: 10 + 15 + 15
    assert unit.status == 'busy' and unit.task['arrived_s'] == 10 + REROUTE_PAUSE_S + 40
    assert [e['kind'] for e in f.log] == ['dispatched', 'blocked', 'arrived']
    assert [(d['version'], d['status']) for d in decisions] == [(1, 'sent'), (1, 'invalidated'), (2, 'sent')]
    assert decisions[0]['id'] == decisions[1]['id'] != decisions[2]['id'] and decisions[1]['invalidated_reason']
    assert len(f.view()[0]['abandoned_routes']) == 1 and f.view()[0]['decision']['reason']
    f.tick(20_000)
    assert len(reports) == 1                             # reported once


def test_known_blockages_are_avoided_from_the_start():
    f, unit, *_ = fleet(blocked=['B_C_0', 'C_B_0'])
    f._beliefs = lambda: {'B_C_0': 'BLOCKED'}            # an agent already believes it
    d = f.dispatch(unit.id, now_s=0, building_id='bldg_x')
    assert d['route_geojson']['properties']['road_ids'] == ['A_D_0', 'D_C_0']
    f.tick(1000)
    assert unit.task['version'] == 1 and unit.status == 'busy'


def test_a_unit_with_no_way_through_gives_up_and_is_free_again():
    f, unit, G, reports, *_ = fleet(blocked=['B_C_0', 'C_B_0', 'D_C_0', 'C_D_0'])
    f.dispatch(unit.id, now_s=0, building_id='bldg_x')
    f.tick(5000)
    assert unit.status == 'available' and [e['kind'] for e in f.log] == ['dispatched', 'blocked', 'stuck']
    assert len(reports) == 2


def test_orders_that_cannot_be_carried_out_are_refused():
    f, unit, *_ = fleet()
    with pytest.raises(DispatchError, match='No unit'):
        f.dispatch('nope', 0, building_id='bldg_x')
    with pytest.raises(DispatchError, match='No building'):
        f.dispatch(unit.id, 0, building_id='bldg_missing')
    with pytest.raises(DispatchError, match='destination'):
        f.dispatch(unit.id, 0)
    f.dispatch(unit.id, 0, building_id='bldg_x')
    with pytest.raises(DispatchError, match='en_route'):
        f.dispatch(unit.id, 1, building_id='bldg_x')
    assert f.available() == [] and f.resource_body(unit, 3)['status'] == 'en_route'
    assert f.resource_body(unit, 3)['skills'] == ['rescue']
