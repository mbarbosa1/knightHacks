"""The response tools: they ask the simulator about units and tell it to dispatch, always for this run and
always for an incident that exists. No model, no database, no network: the simulator and Neon are faked."""
import json
import pytest
from agents import orchestrator, tools as tools_module
from agents.tools import RunTools, ToolError

RUN, INC, UNIT = 'run-1', 'inc-1', 'unit-1'


@pytest.fixture
def tools(monkeypatch):
    t = RunTools.__new__(RunTools)
    t.run_id = RUN
    calls = []
    units = [{'id': UNIT, 'name': 'Engine 1', 'type': 'fire_engine', 'status': 'en_route', 'lon': 139.7, 'lat': 35.69,
              'destination': {'incident_id': INC}, 'helped': 0},
             {'id': 'unit-2', 'name': 'Ambulance 1', 'type': 'ambulance', 'status': 'available', 'lon': 139.71, 'lat': 35.7,
              'destination': None, 'helped': 0}]

    def sim(method, path, body=None):
        calls.append((method, path, body))
        if path == '/clock':
            return {'run_id': RUN}
        if path == '/units':
            return units
        if path == '/tools/nearest-units':
            return [{'unit_id': 'unit-2', 'name': 'Ambulance 1', 'type': 'ambulance', 'skills': ['medical'], 'travel_time_s': 42.0}]
        if path == '/tools/route':
            return {'reachable': True, 'travel_time_s': 42.0, 'length_m': 300, 'road_ids': ['a'] * 50,
                    'geometry': {'coordinates': [[0, 0]] * 500}, 'fallback': None}
        if path == '/dispatch':
            if body['unit_id'] == UNIT:
                raise ToolError('Engine 1 is en_route, not available')
            return {'id': 'dec-1', 'version': 1, 'eta_s': 90.0, 'route_geojson': {'coordinates': [[0, 0]] * 500}}
    monkeypatch.setattr(tools_module, '_sim', sim)
    monkeypatch.setattr(RunTools, '_incident_place', lambda self, incident_id: {'building_id': 'bldg_1'})
    t.calls = calls
    return t


def test_units_are_listed_compactly_and_can_be_filtered(tools):
    assert [u['unit_id'] for u in tools.list_resources()] == [UNIT, 'unit-2']
    assert tools.list_resources(status='available') == [{'unit_id': 'unit-2', 'name': 'Ambulance 1', 'type': 'ambulance',
                                                         'status': 'available', 'longitude': 139.71, 'latitude': 35.7,
                                                         'incident_id': None, 'helped': 0}]
    assert tools.list_resources(type='fire_engine')[0]['incident_id'] == INC


def test_another_runs_units_are_never_shown(tools, monkeypatch):
    monkeypatch.setattr(tools_module, '_sim', lambda method, path, body=None: {'run_id': 'some-other-run'})
    with pytest.raises(ToolError, match='not the one being played'):
        tools.list_resources()


def test_nearest_route_and_dispatch_name_the_run_and_the_place(tools):
    assert tools.nearest_units(INC, type='ambulance', k=2)[0]['travel_time_s'] == 42.0
    assert tools.calls[-1] == ('POST', '/tools/nearest-units', {'building_id': 'bldg_1', 'run_id': RUN, 'k': 2, 'type': 'ambulance'})
    route = tools.plan_route('unit-2', INC)
    assert route == {'reachable': True, 'travel_time_s': 42.0, 'length_m': 300, 'has_fallback': False, 'fallback_travel_time_s': None}
    assert len(json.dumps(route)) < 200                    # no geometry goes to the model
    sent = tools.dispatch_unit('unit-2', INC)
    assert sent == {'dispatched': True, 'decision_id': 'dec-1', 'version': 1, 'unit_id': 'unit-2', 'incident_id': INC, 'eta_s': 90.0}
    assert tools.calls[-1][2] == {'building_id': 'bldg_1', 'run_id': RUN, 'unit_id': 'unit-2', 'incident_id': INC}
    with pytest.raises(ToolError, match='not available'):   # the simulator's reason reaches the model
        tools.dispatch_unit(UNIT, INC)


def test_the_response_picture_puts_incidents_units_and_field_reports_together(tools, monkeypatch):
    monkeypatch.setattr(RunTools, 'list_incidents', lambda self: [
        {'id': INC, 'status': 'corroborated', 'building_id': 'bldg_1', 'location': None,
         'person_groups': [{'condition': 'Trapped', 'est_count': 4, 'needs': ['extrication']},
                           {'condition': 'trapped', 'est_count': 2}, {'condition': 'critical', 'est_count': 1}]},
        {'id': 'inc-2', 'status': 'resolved', 'building_id': None, 'location': None, 'person_groups': None},
        {'id': 'inc-3', 'status': 'reported', 'building_id': 'bldg_2', 'location': None, 'person_groups': None},
        {'id': 'inc-4', 'status': 'reported', 'building_id': None, 'location': None, 'person_groups': None}])
    monkeypatch.setattr(RunTools, 'list_observations', lambda self: [
        {'id': 'o1', 'channel': 'social_media', 'sim_time_s': 5, 'original_text': 'help', 'text_en': 'help'},
        {'id': 'o2', 'channel': 'responder_update', 'sim_time_s': 60, 'text_en': 'road blocked',
         'original_text': json.dumps({'unit_id': UNIT, 'road_id': 'r1', 'road_ids': ['r1'], 'status': 'BLOCKED'})},
        {'id': 'o3', 'channel': 'responder_update', 'sim_time_s': 300, 'text_en': 'Patrol 1 on scene',
         'original_text': json.dumps({'unit_id': 'unit-9', 'incident_id': INC, 'building_id': 'bldg_1', 'action': 'assessed',
                                      'damage_level': 'collapsed', 'trapped': 5, 'critical': 1, 'injured': 2, 'deceased': 0})}])
    assert [r['observation_id'] for r in tools.list_field_reports()] == ['o2', 'o3']
    picture = tools.incidents_to_cover()
    assert picture['free_units'] == {'fire_engine': 0, 'ambulance': 1, 'police': 0}
    first, second = picture['incidents']                    # the resolved one is left out
    assert first['incident_id'] == INC and first['people'] == {'trapped': 6, 'critical': 1}
    assert first['units_on_it'] == [{'unit_id': UNIT, 'type': 'fire_engine', 'status': 'en_route'}]
    assert first['field_reports'][0]['action'] == 'assessed' and first['field_reports'][0]['trapped'] == 5
    # The patrol counted 5 trapped (under 8, and an engine is already on its way) and 1 critical with no ambulance.
    assert first['could_use'] == ['ambulance']
    assert first['nearest_free'] == {'ambulance': [{'unit_id': 'unit-2', 'name': 'Ambulance 1', 'travel_time_s': 42}]}
    assert picture['suggested'] == [{'unit_id': 'unit-2', 'incident_id': INC, 'type': 'ambulance'}]   # the plan code drafts
    # No counts and nobody has looked: a patrol could check it, but none is free.
    assert second['incident_id'] == 'inc-3' and second['could_use'] == ['police'] and second['nearest_free'] == {'police': []}
    assert picture['not_listed'] == {'more_that_could_use_a_unit': 0, 'nothing_needed_now': 1}   # inc-4 has no location


def test_what_an_incident_could_use_follows_the_rescue_rules_and_the_field():
    use = tools_module._could_use
    engine, ambulance = {'type': 'fire_engine'}, {'type': 'ambulance'}
    assert use('reported', {'trapped': 3, 'critical': 1}, [], []) == ['ambulance', 'fire_engine']
    assert use('reported', {'trapped': 3, 'critical': 1}, [engine, ambulance], []) == []
    assert use('reported', {'injured': 9}, [], []) == ['police']                      # "injured" often hides worse: a patrol looks
    assert use('reported', {'injured': 9}, [], [{'unit_type': 'police', 'action': 'assessed', 'trapped': 0, 'critical': 0}]) == []
    assert use('reported', {'safe': 4, 'unhurt_evacuated': 2}, [], []) == []          # everyone accounted for: nobody is sent
    assert use('reported', {'trapped': 12}, [], []) == ['ambulance', 'fire_engine', 'fire_engine']  # two engines for a big one
    assert use('reported', {'trapped': 12}, [engine, ambulance], []) == ['fire_engine']
    assert use('reported', {'trapped': 12}, [engine, engine, ambulance], []) == []
    assert use('disputed', {}, [], []) == ['police'] and use('reported', {}, [], []) == ['police']
    assert use('disputed', {}, [{'type': 'police'}], []) == []
    seen = [{'unit_type': 'police', 'action': 'assessed', 'trapped': 0, 'critical': 2}]
    assert use('reported', {'trapped': 5}, [], seen) == ['ambulance']                  # the patrol's count replaces the estimate
    assert use('reported', {'trapped': 5}, [], [{'unit_type': 'fire_engine', 'action': 'nothing_found'}]) == []
    assert use('reported', {'trapped': 5}, [], [{'unit_type': 'fire_engine', 'action': 'rescued', 'count': 5, 'still_trapped': 0}]) == []
    # Trapped people get an ambulance too, even with nobody reported critical...
    assert use('reported', {'trapped': 4}, [], []) == ['ambulance', 'fire_engine'] and use('reported', {'trapped': 4}, [ambulance], []) == ['fire_engine']
    # ...once: one that came and found nobody to treat, or treated who was there, is not sent again.
    assert use('reported', {'trapped': 4}, [], [{'unit_type': 'ambulance', 'action': 'nothing_found'}]) == ['fire_engine']
    assert use('reported', {'trapped': 4}, [], [{'unit_type': 'ambulance', 'action': 'treated', 'count': 2, 'still_critical': 0}]) == ['fire_engine']


def test_a_list_of_units_is_sent_in_one_call_and_a_unit_cannot_go_twice(tools):
    outcome = tools.dispatch_units([{'unit_id': 'unit-2', 'incident_id': INC}, {'unit_id': 'unit-2', 'incident_id': 'inc-3'},
                                    {'unit_id': UNIT, 'incident_id': INC}, {'unit_id': 'unit-5'}])
    assert outcome['sent'] == [{'unit_id': 'unit-2', 'incident_id': INC, 'eta_s': 90.0, 'decision_id': 'dec-1'}]
    assert [r['reason'] for r in outcome['refused']] == [
        'this unit is already used earlier in this list: a unit goes to one incident',
        'Engine 1 is en_route, not available', 'each assignment needs a unit and an incident']


def test_a_unit_arriving_does_not_wake_the_response_agent_but_a_freed_unit_does(tools, monkeypatch):
    monkeypatch.setattr(RunTools, 'list_incidents', lambda self: [
        {'id': INC, 'status': 'reported', 'building_id': 'bldg_1', 'location': None, 'person_groups': [{'condition': 'trapped', 'est_count': 3}]}])
    monkeypatch.setattr(RunTools, 'list_observations', lambda self: [])
    before = orchestrator._needs(tools.incidents_to_cover())
    units = tools_module._sim('GET', '/units')
    units[0]['status'] = 'busy'                             # the engine has arrived and is working
    assert orchestrator._needs(tools.incidents_to_cover()) == before
    units[0]['status'] = 'available'                        # it is done and free again
    assert orchestrator._needs(tools.incidents_to_cover()) != before


def test_the_orchestrator_records_what_was_really_sent_and_skips_the_model_when_nobody_can_go(tools, monkeypatch):
    monkeypatch.setattr(RunTools, 'list_incidents', lambda self: [
        {'id': INC, 'status': 'reported', 'building_id': 'bldg_1', 'location': None, 'person_groups': [{'condition': 'critical', 'est_count': 2}]}])
    monkeypatch.setattr(RunTools, 'list_observations', lambda self: [])
    logged, asked = [], []
    tools.log_agent_run = lambda *a, **k: logged.append(a)
    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    orch.tools, orch.run_id, orch.cursor, orch._responded_to = tools, RUN, 'obs-9', None
    orch._responding, orch._stop_responding = orchestrator.threading.Lock(), orchestrator.threading.Event()
    orch.response = {'response_agent': ('instructions', ['dispatch_units'])}

    def fake_model(instructions, payload, specs, functions, max_steps):
        asked.append(payload)
        assert payload['suggested'] == [{'unit': 'A1', 'incident': 'I1'}]              # short names, not 36-character ids
        said = functions['dispatch_units'](payload['suggested'])
        assert said == {'sent': [{'unit': 'A1', 'incident': 'I1', 'eta_s': 90.0}], 'refused': []}
        return {'output': {'summary': 'Sent three ambulances.', 'not_covered': []}, 'tool_calls': [], 'steps': 2}
    monkeypatch.setattr(orchestrator, 'run_agent', fake_model)
    result = orch.respond()
    assert asked[0]['incidents'][0]['nearest_free'] == {'ambulance': [{'unit': 'A1', 'travel_time_s': 42}]}
    assert [d['unit_id'] for d in result['output']['dispatched']] == ['unit-2']       # one, whatever its summary claims
    assert logged[0][0] == 'response_agent' and logged[0][2]['dispatched'] == result['output']['dispatched']

    tools_module._sim('GET', '/units')[1]['status'] = 'busy'                          # now no ambulance is free
    assert orch.respond() is None and len(asked) == 1


def test_the_response_agent_looks_on_its_own_and_never_runs_two_rounds_at_once(tools, monkeypatch):
    import threading, time
    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    orch.run_id, orch.response = RUN, {'response_agent': ('instructions', ['dispatch_units'])}
    orch._responding, orch._stop_responding = threading.Lock(), threading.Event()
    looks, inside = [], threading.Event()

    def slow_round():
        looks.append(time.monotonic())
        inside.set()
        time.sleep(0.15)
        return 'done'
    orch._respond = slow_round
    first = threading.Thread(target=orch.respond)
    first.start()
    inside.wait(1)
    assert orch.respond() is None and len(looks) == 1     # a round is under way: this look is skipped, not queued
    first.join()

    orch.keep_responding(interval_s=0.05)                  # on its own thread, with nobody calling it
    time.sleep(0.5)
    orch.stop_responding()
    seen = len(looks)
    assert seen >= 3
    time.sleep(0.3)
    assert len(looks) <= seen + 1                          # and it stops when the run is no longer worked on
