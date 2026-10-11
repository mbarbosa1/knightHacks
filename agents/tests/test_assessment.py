"""Assessment folder: detect and medical agent files, and the assessment coordinator. No model calls."""
import pytest
from agents import runner
from agents.loader import load_agent
from agents.tools import RunTools, TOOL_SPECS

detect = load_agent('assesmentAgent/detect-agent/detect_agent.py')
medical = load_agent('assesmentAgent/medical-agent/medical_agent.py')
coordinator = load_agent('assesmentAgent/assessment-coordinator/assessment_coordinator.py')


@pytest.mark.parametrize('agent', [detect, medical], ids=['detect', 'medical'])
def test_agent_file_is_complete(agent):
    assert agent.INSTRUCTIONS.exists() and agent.INSTRUCTIONS.parent.name == agent.AGENT.replace('_', '-')
    assert all(t in TOOL_SPECS and hasattr(RunTools, t) for t in agent.TOOLS)
    assert 'log_agent_run' in agent.TOOLS and 'get_run' in agent.TOOLS


def test_only_detect_can_mark_roads():
    assert 'set_road_restriction' in detect.TOOLS and 'set_road_restriction' not in medical.TOOLS


class Tools:
    run_id = 'run'

    def __init__(self):
        self.logged = []

    def toolbox(self, agent, names, done):
        return RunTools.toolbox(RunTools.__new__(RunTools), agent, ['log_agent_run'], done)

    def log_agent_run(self, agent, input_ref, output, duration_ms=None):
        self.logged.append((agent, input_ref, output))


def test_agent_run_sends_the_batch_and_logs_the_answer(monkeypatch):
    seen = {}

    def model(instructions, payload, specs, functions):
        seen.update(instructions=instructions, payload=payload)
        functions['log_agent_run'](input_ref='o2')
        return {'output': {'incidents': []}, 'tool_calls': [], 'steps': 3}
    monkeypatch.setattr(runner, 'run_agent', model)
    tools, batch = Tools(), [{'observation_id': 'o1'}, {'observation_id': 'o2'}]
    result = medical.run(tools, batch, cursor='o0')
    assert seen['instructions'].startswith('# Medical') and seen['payload'] == {
        'run_id': 'run', 'observation_ids': ['o1', 'o2'], 'cursor': 'o0', 'normalized': batch}
    assert result['output'] == {'incidents': []} and tools.logged == [('medical_agent', 'o2', {'incidents': []})]


def test_agent_failure_is_logged_not_raised(monkeypatch):
    def model(*a):
        raise RuntimeError('model down')
    monkeypatch.setattr(runner, 'run_agent', model)
    tools = Tools()
    result = detect.run(tools, [{'observation_id': 'o1'}])
    assert result['error'] == 'RuntimeError: model down'
    assert tools.logged == [('detect_agent', 'o1', {'error': 'RuntimeError: model down'})]


# ---- assessment coordinator --------------------------------------------------------

def result(**output):
    return {'output': output, 'tool_calls': [], 'steps': 1, 'seconds': 1}


DETECT = result(situation_summary='A shop-house collapsed.', road_restrictions=[{'road_id': 'r1', 'status': 'BLOCKED'}],
                incidents=[{'incident_id': 'A', 'change': 'updated', 'status': 'corroborated', 'priority': 'high',
                            'building_id': 'bldg_1', 'prediction': 'more collapse likely', 'location': {'longitude': 1, 'latitude': 2}},
                           {'incident_id': 'B', 'change': 'created', 'status': 'reported', 'priority': 'low'}])
MEDICAL = result(situation_summary='3 injured.', totals={'injured': 3},
                 incidents=[{'incident_id': 'A', 'change': 'updated', 'medical_priority': 'critical', 'needs_ambulances': 1,
                             'person_groups': [{'group_id': 'g1', 'est_count': 3, 'condition': 'injured'}]},
                            {'incident_id': 'C', 'change': 'created', 'medical_priority': 'medium'},
                            {'change': 'created'}])
BATCH = [{'observation_id': 'o1'}, {'observation_id': 'o2'}]


def test_merge_joins_both_agents_per_incident():
    out = coordinator.merge({'detect_agent': DETECT, 'medical_agent': MEDICAL}, BATCH)
    a = out['incidents'][0]
    assert a['incident_id'] == 'A' and a['priority'] == 'critical'            # the more urgent of high / critical
    assert a['building_id'] == 'bldg_1' and a['prediction'] == 'more collapse likely'   # from detect
    assert a['person_groups'][0]['est_count'] == 3 and a['needs_ambulances'] == 1        # from medical
    assert a['changed_by'] == ['detect', 'medical']


def test_merge_orders_by_urgency_and_lists_what_needs_a_response():
    out = coordinator.merge({'detect_agent': DETECT, 'medical_agent': MEDICAL}, BATCH)
    assert [i['incident_id'] for i in out['incidents']] == ['A', 'C', 'B']
    assert out['needs_response'] == ['A']
    assert out['cursor'] == 'o2' and out['observation_ids'] == ['o1', 'o2']
    assert out['road_restrictions'] == [{'road_id': 'r1', 'status': 'BLOCKED'}] and out['totals'] == {'injured': 3}
    assert out['situation_summary'] == 'A shop-house collapsed. 3 injured.' and out['unidentified_incidents'] == 1


def test_merge_with_a_failed_agent():
    out = coordinator.merge({'detect_agent': {'error': 'LLMError: down', 'seconds': 1}, 'medical_agent': MEDICAL}, BATCH)
    assert out['agents'] == {'detect_agent': 'LLMError: down', 'medical_agent': 'ok'}
    assert [i['incident_id'] for i in out['incidents']] == ['A', 'C'] and out['road_restrictions'] == []


def test_merge_with_nothing():
    out = coordinator.merge({}, [])
    assert out['incidents'] == [] and out['cursor'] is None and out['situation_summary'] is None
