"""The pieces fit together: every agent the orchestrator runs has its .md, every tool it may use exists and is
described for the model, the .md files name only tools the agent gets, and write tools reject bad input
before touching the database."""
import re
import pytest
from agents import orchestrator, runner
from agents.tools import RunTools, TOOL_SPECS, ToolError


ALL_AGENTS = {**orchestrator.ASSESSMENT_AGENTS, **orchestrator.RESPONSE_AGENTS}


@pytest.mark.parametrize('name', sorted(ALL_AGENTS))
def test_agent_instructions_exist(name):
    path, _ = ALL_AGENTS[name]
    assert (orchestrator.AGENTS_DIR / path).exists(), f'{name}: {path} not found'


@pytest.mark.parametrize('name', sorted(ALL_AGENTS))
def test_agent_tools_exist(name):
    _, tools = ALL_AGENTS[name]
    for tool in tools:
        assert tool in TOOL_SPECS, f'{name}: {tool} has no description for the model'
        assert hasattr(RunTools, tool), f'{name}: {tool} is not implemented in tools.py'


@pytest.mark.parametrize('name', sorted(ALL_AGENTS))
def test_md_names_only_tools_the_agent_gets(name):
    path, tools = ALL_AGENTS[name]
    in_md = set(re.findall(r'^\| `(\w+)\(', (orchestrator.AGENTS_DIR / path).read_text(), re.M))
    assert in_md, f'{name}: no tools table found in its .md'
    assert in_md <= set(tools), f'{name}: .md mentions tools it is not given: {sorted(in_md - set(tools))}'


def test_in_agent_loads_from_its_folder():
    assert orchestrator.IN_AGENT == 'in_agent'
    assert orchestrator.InAgent.__name__ == 'InAgent'


def test_tool_specs_are_valid_for_the_model():
    for name, (description, params, required) in TOOL_SPECS.items():
        assert description and isinstance(params, dict)
        assert set(required) <= set(params), f'{name}: required parameter not defined'


def test_toolbox_binds_the_agent_name():
    tools = RunTools.__new__(RunTools)
    seen, done = {}, {}
    tools.get_last_agent_run = lambda agent: seen.setdefault('last', agent)
    specs, functions = tools.toolbox('medical_agent', ['get_last_agent_run', 'log_agent_run'], done)
    assert [s['function']['name'] for s in specs] == ['get_last_agent_run', 'log_agent_run']
    functions['get_last_agent_run']()
    assert seen == {'last': 'medical_agent'}


def test_log_agent_run_tool_only_marks_progress():
    """The model must not put its whole answer in a tool call: that crashed the model server
    ("json parser could not parse region as JSON"). The tool takes only input_ref."""
    assert TOOL_SPECS['log_agent_run'][2] == ['input_ref'] and 'output' not in TOOL_SPECS['log_agent_run'][1]
    done = {}
    _, functions = RunTools.__new__(RunTools).toolbox('detect_agent', ['log_agent_run'], done)
    reply = functions['log_agent_run'](input_ref='obs9')
    assert done == {'input_ref': 'obs9'} and 'final JSON answer' in reply['next']
    functions['log_agent_run'](input_ref='obs9', output={'incidents': []})   # an old-style call still works
    assert done['output'] == {'incidents': []}


class FakeTools:
    run_id = 'run'

    def __init__(self):
        self.logged = []

    def toolbox(self, agent, names, done):
        return RunTools.toolbox(RunTools.__new__(RunTools), agent, ['log_agent_run'], done)

    def log_agent_run(self, agent, input_ref, output, duration_ms=None, run_key=None):
        self.logged.append((agent, input_ref, output))
        return {'id': f'{agent}-audit'}


def fake_orchestrator(agents):
    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    orch.run_id, orch.cursor, orch.tools = 'run', None, FakeTools()
    orch.assessment = {name: ('instructions', ['log_agent_run']) for name in agents}
    orch.response, orch._responded_to, orch._respond_lock = {}, None, __import__('threading').Lock()
    orch.medical_queue, orch._queue_lock = [], __import__('threading').Lock()
    return orch


def test_orchestrator_logs_each_agents_final_answer(monkeypatch):
    def agent(instructions, payload, specs, functions):
        functions['log_agent_run'](input_ref='obs2')
        return {'output': {'incidents': [{'incident_id': 'i1'}], 'situation_summary': 'ok'}
                if 'detect' in instructions else {'places': [], 'situation_summary': 'ok'},
                'tool_calls': [], 'steps': 2}
    monkeypatch.setattr(runner, 'run_agent', agent)
    orch = fake_orchestrator(['detect_agent', 'medical_agent'])
    orch.assessment['detect_agent'] = ('detect', ['log_agent_run'])
    results = orch.assess([{'observation_id': 'obs1'}, {'observation_id': 'obs2'}])
    assert set(results) == {'detect_agent', 'medical_agent'} and orch.cursor == 'obs2'
    assert sorted(orch.tools.logged) == [
        ('detect_agent', 'obs2', {'incidents': [{'incident_id': 'i1'}], 'situation_summary': 'ok'}),
        ('medical_agent', 'obs2', {'places': [], 'situation_summary': 'ok'})]
    assert results['medical_agent']['audit_id'] == 'medical_agent-audit'


def test_orchestrator_logs_a_failed_agent_and_keeps_the_others(monkeypatch):
    def agent(instructions, payload, specs, functions):
        if 'boom' in instructions:
            raise RuntimeError('model server error 500')
        return {'output': {'places': []}, 'tool_calls': [], 'steps': 1}
    monkeypatch.setattr(runner, 'run_agent', agent)
    orch = fake_orchestrator(['detect_agent', 'medical_agent'])
    orch.assessment['detect_agent'] = ('boom', ['log_agent_run'])
    results = orch.assess([{'observation_id': 'obs1'}])
    assert 'error' in results['detect_agent'] and 'error' not in results['medical_agent']
    logged = dict((a, out) for a, _, out in orch.tools.logged)
    assert logged['detect_agent'] == {'error': 'RuntimeError: model server error 500'}
    assert logged['medical_agent'] == {'places': [], 'situation_summary': ''}


# ---- write tools check their input before any database call ----------------------

@pytest.fixture
def offline_tools():
    tools = RunTools.__new__(RunTools)
    tools.run_id = '00000000-0000-0000-0000-000000000000'
    tools.map = type('Map', (), {'roads': {'1_2_0': {}}})()
    return tools


@pytest.mark.parametrize('kwargs, message', [
    ({'status': 'maybe'}, 'status must be one of'),
    ({'status': 'reported', 'person_groups': [{'group_id': 'g'}, {'group_id': 'g'}]}, 'own group_id'),
    ({'status': 'reported', 'person_groups': [{'est_count': 2}]}, 'own group_id'),
    ({'status': 'reported', 'location': {'longitude': 139.7}}, 'both longitude and latitude'),
])
def test_upsert_incident_rejects_bad_input(offline_tools, kwargs, message):
    with pytest.raises(ToolError, match=message):
        offline_tools.upsert_incident(**kwargs)


def test_set_road_restriction_rejects_bad_input(offline_tools):
    with pytest.raises(ToolError, match='status must be one of'):
        offline_tools.set_road_restriction('1_2_0', 'CLOSED')
    with pytest.raises(ToolError, match='not on this run'):
        offline_tools.set_road_restriction('9_9_9', 'BLOCKED')


# ---- orchestrator bookkeeping ------------------------------------------------------

def test_summary_counts_writes_and_errors():
    result = {'seconds': 2, 'steps': 4, 'output': {'incidents': [1, 2], 'situation_summary': 'two'}, 'tool_calls': [
        {'tool': 'upsert_incident', 'args': {}, 'ok': True},
        {'tool': 'set_road_restriction', 'args': {}, 'ok': False, 'error': 'ToolError: road'},
        {'tool': 'log_agent_run', 'args': {'input_ref': 'o'}, 'ok': True}]}
    s = orchestrator._summary(result)
    assert s['incidents'] == 2 and s['summary'] == 'two' and s['writes_ok'] == 1 and s['tool_errors'] == ['ToolError: road']


def test_summary_of_a_failed_agent():
    assert orchestrator._summary({'error': 'LLMError: down', 'seconds': 1}) == {'error': 'LLMError: down', 'seconds': 1}


def test_merge_groups_keeps_both_writers_groups():
    from agents.tools import _merge_groups
    old = [{'group_id': 'g1', 'est_count': 2}, {'group_id': 'g2', 'est_count': 1}]
    assert _merge_groups(old, None) == old
    merged = _merge_groups(old, [{'group_id': 'g1', 'est_count': 5}, {'group_id': 'g3', 'est_count': 4}])
    assert {g['group_id']: g['est_count'] for g in merged} == {'g1': 5, 'g2': 1, 'g3': 4}


def test_upsert_incident_rejects_made_up_incident_ids(offline_tools):
    with pytest.raises(ToolError, match="'inc_1' is not an incident id"):
        offline_tools.upsert_incident('reported', incident_id='inc_1')


def test_upsert_incident_accepts_extra_fields_the_model_adds():
    """Seen live: the hosted model passed prediction=... to upsert_incident (a TypeError before)."""
    import inspect
    assert any(p.kind is p.VAR_KEYWORD for p in inspect.signature(RunTools.upsert_incident).parameters.values())
