"""Medical output validation and queue regression checks; no live services."""
import pytest
from pydantic import ValidationError
from agents import orchestrator as mod
from test_wiring import fake_orchestrator


def place(**changes):
    return {'name': 'Reported location', 'building_id': None, 'location': None,
            'trapped': None, 'critical': None, 'injured': None, 'priority': 1,
            'why': 'People reported trapped', 'observation_ids': ['obs1'], **changes}


def queue():
    orch = mod.Orchestrator.__new__(mod.Orchestrator)
    orch.medical_queue = []
    return orch


def test_numeric_strings_and_unknown_counts():
    orch = queue()
    orch.add_to_medical_queue(place(trapped='3', priority='1',
                                    location={'longitude': '139.7', 'latitude': '35.7'}))
    assert orch.medical_queue[0]['trapped'] == 3
    assert orch.medical_queue[0]['critical'] is None
    assert orch.medical_queue[0]['location'] == {'longitude': 139.7, 'latitude': 35.7}


def test_location_without_coordinates_means_no_location():
    # What the model sends for a vague report ("somewhere around Kabukicho"); seen live, it rejected a whole batch.
    orch = queue()
    orch.add_to_medical_queue(place(trapped=1, location={'longitude': None, 'latitude': None}))
    assert orch.medical_queue[0]['location'] is None


@pytest.mark.parametrize('changes', [
    {'location': {'longitude': 139.7, 'latitude': None}},
    {'trapped': -1}, {'critical': True}, {'injured': 1.5}, {'trapped': 'several'},
    {'priority': 4}, {'observation_ids': 'obs1'}, {'observation_ids': []},
    {'location': {'longitude': 181, 'latitude': 35}},
    {'location': {'longitude': 139, 'latitude': float('nan')}},
    {'location': {'longitude': True, 'latitude': 35}},
])
def test_bad_place_does_not_change_queue(changes):
    orch = queue()
    with pytest.raises(ValidationError):
        orch.add_to_medical_queue(place(**changes))
    assert orch.medical_queue == []


def test_merge_keeps_max_count_sources_and_urgent_reason():
    orch = queue()
    loc = {'longitude': 139.7, 'latitude': 35.7}
    orch.add_to_medical_queue(place(trapped=3, location=loc))
    orch.add_to_medical_queue(place(trapped=2, injured=1, priority=2, location=loc,
                                    why='Less urgent update', observation_ids=['obs2']))
    assert len(orch.medical_queue) == 1
    assert orch.medical_queue[0]['trapped'] == 3
    assert orch.medical_queue[0]['observation_ids'] == ['obs1', 'obs2']
    assert orch.medical_queue[0]['why'] == 'People reported trapped'


def test_unknown_count_still_has_priority():
    orch = queue()
    orch.add_to_medical_queue(place(priority=2, injured=1))
    orch.add_to_medical_queue(place())
    assert orch.medical_queue[0]['priority'] == 1


@pytest.mark.parametrize('output', [
    None, {'places': None}, {'places': [None]}, {'places': [place(trapped='many')]},
    {'places': [place(observation_ids=['unknown'])]},
])
def test_invalid_medical_result_is_logged_without_stopping_detection(monkeypatch, output):
    def agent(instructions, *args):
        return {'output': output if instructions == 'medical' else {'incidents': []},
                'tool_calls': [], 'steps': 1}
    monkeypatch.setattr(mod, 'run_agent', agent)
    orch = fake_orchestrator(['detect_agent', 'medical_agent'])
    orch.assessment['medical_agent'] = ('medical', [])
    orch.medical_queue = []
    results = orch.assess([{'observation_id': 'obs1'}])
    assert 'error' in results['medical_agent']
    assert 'error' not in results['detect_agent']
    logged = {name: result for name, ref, result in orch.tools.logged}
    assert 'error' in logged['medical_agent']
    assert orch.medical_queue == []


def test_medical_has_no_log_tool_but_is_validated_saved_and_queued(monkeypatch):
    def agent(instructions, payload, specs, functions):
        assert 'log_agent_run' not in functions
        return {'output': {'places': [place(trapped='3')]}, 'tool_calls': [], 'steps': 1}
    monkeypatch.setattr(mod, 'run_agent', agent)
    orch = fake_orchestrator(['medical_agent'])
    # Use the actual read-only tool allowlist, not the legacy fake log tool.
    orch.tools.toolbox = lambda *args: ([], {})
    orch.medical_queue = []
    result = orch.assess([{'observation_id': 'obs1'}])['medical_agent']
    assert 'error' not in result
    assert result['audit_id'] == 'medical_agent-audit'
    assert orch.tools.logged[0][2]['places'][0]['trapped'] == 3
    assert orch.medical_queue[0]['trapped'] == 3


def test_building_must_be_a_precise_candidate_from_its_sources():
    batch = [{'observation_id': 'obs1', 'location': {'precision': 'gps',
              'building_candidates': [{'building_id': 'b1'}]}}]
    for building, expected in [('b1', 'b1'), ('invented', None)]:
        result = mod.validate_medical_output({'places': [place(building_id=building)]}, batch)
        assert result['places'][0]['building_id'] == expected
    batch[0]['location']['precision'] = 'landmark'
    assert mod.validate_medical_output({'places': [place(building_id='b1')]}, batch)['places'][0]['building_id'] is None
