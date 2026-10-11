import json, pathlib, sys, uuid
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from shapely.geometry import Point
from sim.routing import Router
from sim.units import DispatchError, Fleet, REROUTE_PAUSE_S
from tests.test_routing import toy

RUN = '00000000-0000-4000-8000-000000000001'


def fleet(blocked=(), limited=(), types=('fire_engine',), people=None, damage='collapsed'):
    """Toy map: A -10s-> B -10s-> C direct, A -15s-> D -15s-> C the long way. One engine parked at A.
    people: {condition: count} really at the building."""
    G = toy()
    status = {**{r: {'status': 'BLOCKED', 'sim_time_s': 0.0} for r in blocked}, **{r: {'status': 'LIMITED', 'sim_time_s': 0.0} for r in limited}}
    links, reports, beliefs_written = [], [], []
    truth = SimpleNamespace(road_status=status, add_link=lambda *a: links.append(a), people={}, changes=[],
                            building_damage={'bldg_x': {'damage_level': damage}})
    def set_people(gid, bid, count, condition, mobility, language, at_s):
        truth.people[gid] = {'building_id': bid, 'count': count, 'condition': condition, 'mobility': mobility, 'language': language}
        truth.changes.append((at_s, condition, count))
    truth.set_people = set_people
    for k, (condition, count) in enumerate((people or {}).items()):
        set_people(f'bldg_x:g{k}', 'bldg_x', count, condition, 'immobile', 'ja', 0.0)
    truth.changes.clear()
    c = G.nodes['C']
    m = SimpleNamespace(facilities={'f1': {'access_node': 'A'}}, roads={'B_C_0': {'name_en': 'Yasukuni Street'}},
                        building_geoms={'bldg_x': Point(c['x'], c['y'])})
    a = G.nodes['A']
    resources = [{'id': str(uuid.uuid5(uuid.NAMESPACE_DNS, f'{t}{i}')), 'type': t, 'name': f"{t.replace('_', ' ').title()} {i + 1}",
                  'home_facility_id': 'f1', 'location': {'longitude': a['x'], 'latitude': a['y']}} for i, t in enumerate(types)]
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
    f.tick(150)
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
    f.tick(10 + REROUTE_PAUSE_S + 45)                    # back B -> A -> D -> C: 10 + 15 + 15
    assert unit.status == 'busy' and unit.task['arrived_s'] == 10 + REROUTE_PAUSE_S + 40
    assert [e['kind'] for e in f.log] == ['dispatched', 'blocked', 'arrived']
    assert [(d['version'], d['status']) for d in decisions] == [(1, 'sent'), (1, 'invalidated'), (2, 'sent')]
    assert decisions[0]['id'] == decisions[1]['id'] != decisions[2]['id'] and decisions[1]['invalidated_reason']
    assert len(f.view()[0]['abandoned_routes']) == 1 and f.view()[0]['decision']['reason']
    f.tick(20_000)
    assert len([r for r in reports if 'BLOCKED' in r['original_text']]) == 1   # reported once


def test_known_blockages_are_avoided_from_the_start():
    f, unit, *_ = fleet(blocked=['B_C_0', 'C_B_0'])
    f._beliefs = lambda: {'B_C_0': 'BLOCKED'}            # an agent already believes it
    d = f.dispatch(unit.id, now_s=0, building_id='bldg_x')
    assert d['route_geojson']['properties']['road_ids'] == ['A_D_0', 'D_C_0']
    f.tick(35)
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


def test_an_engine_frees_the_trapped_one_by_one_then_reports_and_is_free():
    f, unit, G, reports, links, _ = fleet(people={'trapped': 3, 'critical': 2})
    f.dispatch(unit.id, now_s=0, building_id='bldg_x')   # arrives at 20 s; 60 s per person in a collapsed building
    f.tick(79)
    assert unit.status == 'busy' and f.outcomes == {'rescued': 0, 'treated': 0, 'died_waiting': 0}
    f.tick(81)
    assert f.outcomes['rescued'] == 1 and f.truth.people['bldg_x:g0']['count'] == 2
    f.tick(500)
    assert unit.status == 'available' and f.outcomes == {'rescued': 3, 'treated': 0, 'died_waiting': 0}
    assert f.truth.people['bldg_x:g0']['count'] == 0 and f.truth.people['bldg_x:rescued'] ['count'] == 3
    assert f.truth.people['bldg_x:g1']['count'] == 2                      # an engine does not treat the critical
    assert [c[0] for c in f.truth.changes if c[1] == 'rescued'] == [80, 140, 200]
    said = json.loads(reports[-1]['original_text'])
    assert said == {'unit_id': unit.id, 'incident_id': None, 'building_id': 'bldg_x', 'action': 'rescued', 'count': 3, 'still_trapped': 0}
    assert reports[-1]['sim_time_s'] == 200 and '3 people freed' in reports[-1]['text_en'] and links[-1][2:4] == ('building', 'bldg_x')
    assert f.view()[0]['helped'] == 3 and [e['kind'] for e in f.log] == ['dispatched', 'arrived', 'left']

    f.dispatch(unit.id, now_s=300, building_id='bldg_x')                  # free again: it can be sent on a new job
    assert unit.status == 'en_route' and f.view()[0]['abandoned_routes'] == [] and f.view()[0]['helped'] == 0


def test_two_engines_work_twice_as_fast_and_a_less_damaged_building_is_quicker():
    f, *_ = fleet(types=('fire_engine', 'fire_engine'), people={'trapped': 4})
    for unit in f.units.values():
        f.dispatch(unit.id, now_s=0, building_id='bldg_x')
    f.tick(10_000)
    assert max(c[0] for c in f.truth.changes) == 140 and f.outcomes['rescued'] == 4      # one engine alone: 260
    assert sorted(u.task['work']['done'] for u in f.units.values()) == [2, 2]
    assert {u.status for u in f.units.values()} == {'available'}

    f, unit, *_ = fleet(people={'trapped': 4}, damage='moderate')
    f.dispatch(unit.id, now_s=0, building_id='bldg_x')
    f.tick(10_000)
    assert max(c[0] for c in f.truth.changes) == 20 + 4 * 20


def test_an_ambulance_treats_the_critical_and_the_wrong_unit_finds_nothing_to_do():
    f, unit, G, reports, *_ = fleet(types=('ambulance',), people={'trapped': 2, 'critical': 2})
    f.dispatch(unit.id, now_s=0, building_id='bldg_x')
    f.tick(500)
    assert f.outcomes == {'rescued': 0, 'treated': 2, 'died_waiting': 0} and unit.task['left_s'] == 20 + 2 * 30
    assert f.truth.people['bldg_x:treated']['count'] == 2 and f.truth.people['bldg_x:g0']['count'] == 2

    f, unit, G, reports, *_ = fleet(types=('ambulance',), people={'trapped': 5})
    f.dispatch(unit.id, now_s=0, building_id='bldg_x')
    f.tick(79)
    assert unit.status == 'busy'                                           # a minute is lost finding that out
    f.tick(81)
    assert unit.status == 'available' and f.outcomes == {'rescued': 0, 'treated': 0, 'died_waiting': 0}
    assert json.loads(reports[-1]['original_text'])['action'] == 'nothing_found' and f.truth.changes == []


def test_a_patrol_looks_and_reports_what_is_really_there():
    f, unit, G, reports, *_ = fleet(types=('police',), people={'trapped': 5, 'critical': 1, 'injured': 7})
    f.dispatch(unit.id, now_s=0, building_id='bldg_x')
    f.tick(139)
    assert unit.status == 'busy' and reports == []
    f.tick(141)
    assert unit.status == 'available' and f.truth.changes == []            # it helps nobody itself
    assert json.loads(reports[0]['original_text']) == {'unit_id': unit.id, 'incident_id': None, 'building_id': 'bldg_x', 'action': 'assessed',
                                                       'damage_level': 'collapsed', 'trapped': 5, 'critical': 1, 'injured': 7, 'deceased': 0}
    assert 'building collapsed' in reports[0]['text_en'] and '5 trapped' in reports[0]['text_en']


def test_people_left_waiting_get_worse_unless_the_right_unit_is_working_there():
    f, unit, *_ = fleet(people={'trapped': 6, 'critical': 5})           # nobody is sent
    f.tick(599)
    assert f.truth.changes == []
    f.tick(600)                                                           # a fifth (rounded up) get worse
    count = lambda condition: next((g['count'] for g in f.truth.people.values() if g['condition'] == condition), 0)
    assert (count('trapped'), count('critical'), count('deceased')) == (4, 6, 1) and f.outcomes['died_waiting'] == 1
    f.tick(900)
    assert (count('trapped'), count('critical'), count('deceased')) == (3, 5, 3)

    f, unit, *_ = fleet(people={'trapped': 30, 'critical': 5})           # an engine at work: the trapped hold on
    f.dispatch(unit.id, now_s=0, building_id='bldg_x')
    f.tick(600)
    assert count('trapped') == 30 - 9 and count('critical') == 4 and count('deceased') == 1   # 9 freed; critical untreated

    f, unit, *_ = fleet(people={'trapped': 1})                            # a lone trapped person: critical, then dead
    f.tick(900)
    assert (count('trapped'), count('critical'), count('deceased')) == (0, 0, 1)
