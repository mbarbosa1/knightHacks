"""The orchestrator's queue (both stages remembered in Neon) and the response-side tools. No model, no database."""
import httpx
import pytest
from agents import orchestrator, runner, tools as tools_mod
from agents.tools import RunTools, ToolError


# ---- queue -------------------------------------------------------------------------

class QueueTools:
    run_id = 'run'

    def __init__(self, records, assessed=()):
        self.records, self.assessed, self.logged = records, set(assessed), []

    def agent_outputs(self, agent):
        return self.records

    def assessed_observations(self):
        return self.assessed

    def processed_inputs(self, agent):
        return {r['observation_id'] for r in self.records}

    def list_observations(self):
        return [{'id': r['observation_id'], 'sim_time_s': r['sim_time_s']} for r in self.records]

    def toolbox(self, agent, names, done):
        return RunTools.toolbox(RunTools.__new__(RunTools), agent, ['log_agent_run'], done)

    def log_agent_run(self, agent, input_ref, output, duration_ms=None, run_key=None):
        self.logged.append((agent, output))
        if agent == 'orchestrator' and output.get('complete'):
            self.assessed |= set(output['observation_ids'])


def queue_orchestrator(records, assessed=()):
    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    orch.run_id, orch.cursor, orch.batch_size, orch.in_workers, orch.parallel_batches = 'run', None, 2, 1, 3
    orch.tools = QueueTools(records, assessed)
    orch.assessment = {n: ('instructions', ['log_agent_run']) for n in ('detect_agent', 'medical_agent')}
    return orch


RECORDS = [{'observation_id': f'o{i}', 'sim_time_s': t} for i, t in ((1, 30), (2, 10), (3, 20))]


def test_pending_assessment_skips_done_and_sorts_by_time():
    orch = queue_orchestrator(RECORDS, assessed={'o3'})
    assert [r['observation_id'] for r in orch.pending_assessment()] == ['o2', 'o1']


def test_records_left_from_an_earlier_run_are_assessed(monkeypatch):
    monkeypatch.setattr(runner, 'run_agent', lambda *a: {'output': {'incidents': []}, 'tool_calls': [], 'steps': 1})
    orch = queue_orchestrator(RECORDS)          # in_agent already did them; nothing assessed yet
    assert orch.cycle() == 3 and orch.tools.assessed == {'o1', 'o2', 'o3'}
    assert orch.cycle() == 0                     # nothing left


def test_a_batch_where_every_agent_failed_stays_in_the_queue(monkeypatch):
    def down(*a):
        raise RuntimeError('model down')
    monkeypatch.setattr(runner, 'run_agent', down)
    orch = queue_orchestrator(RECORDS)
    assert orch.cycle() == 0 and orch.tools.assessed == set()
    logged = [o for a, o in orch.tools.logged if a == 'orchestrator']
    assert len(logged) == 2 and not any(o['complete'] for o in logged)   # both batches tried, both kept queued
    monkeypatch.setattr(runner, 'run_agent', lambda *a: {'output': {}, 'tool_calls': [], 'steps': 1})
    assert orch.cycle() == 3                                          # model back: retried


def test_one_agent_failing_still_completes_the_batch(monkeypatch):
    def half(instructions, payload, specs, functions):
        if half.calls.append(1) or len(half.calls) % 2:
            raise RuntimeError('one down')
        return {'output': {}, 'tool_calls': [], 'steps': 1}
    half.calls = []
    monkeypatch.setattr(runner, 'run_agent', half)
    orch = queue_orchestrator(RECORDS[:2])
    assert orch.cycle() == 2


# ---- response tools ------------------------------------------------------------------

@pytest.fixture
def rt(monkeypatch):
    t = RunTools.__new__(RunTools)
    t.run_id = 'run'
    t.map = type('Map', (), {'buildings': {'b1': {}}, 'facilities': {
        'h1': {'type': 'hospital', 'lon': 1.0, 'lat': 2.0, 'name_en': 'H1', 'capacity': None},
        'h2': {'type': 'hospital', 'lon': 3.0, 'lat': 4.0, 'name_en': 'H2', 'capacity': 50},
        's1': {'type': 'shelter', 'lon': 5.0, 'lat': 6.0, 'name_en': 'S1', 'capacity': None}}})()
    t.list_incidents = lambda: [{'id': 'i1', 'building_id': 'b1', 'location': {'longitude': 9.0, 'latitude': 8.0}},
                                {'id': 'i2', 'building_id': None, 'location': {'longitude': 7.0, 'latitude': 6.0}}]
    return t


def test_places_turn_into_simulator_fields(rt):
    assert rt._place({'incident_id': 'i1'}) == {'building_id': 'b1'}
    assert rt._place({'incident_id': 'i2'}) == {'lon': 7.0, 'lat': 6.0}
    assert rt._place({'incident_id': 'i1'}, prefix='from_') == {'from_lon': 9.0, 'from_lat': 8.0}
    assert rt._place({'facility_id': 'h2'}) == {'lon': 3.0, 'lat': 4.0}
    assert rt._place({'unit_id': 'u1'}, prefix='from_') == {'unit_id': 'u1'}
    assert rt._place({'longitude': 1, 'latitude': 2}) == {'lon': 1, 'lat': 2}
    for bad in ({'incident_id': 'nope'}, {'facility_id': 'nope'}, {'building_id': 'nope'}, {}):
        with pytest.raises(ToolError):
            rt._place(bad)


def test_simulator_refusals_become_tool_errors(monkeypatch):
    def refuse(method, url, **kw):
        return httpx.Response(409, json={'detail': 'unit u1 is busy'}, request=httpx.Request(method, url))
    monkeypatch.setattr(tools_mod.httpx, 'request', refuse)
    with pytest.raises(ToolError, match='unit u1 is busy'):
        tools_mod._sim('POST', '/tools/route', {'unit_id': 'u1'})


def test_simulator_down_is_a_clear_tool_error(monkeypatch):
    def down(method, url, **kw):
        raise httpx.ConnectError('refused')
    monkeypatch.setattr(tools_mod.httpx, 'request', down)
    with pytest.raises(ToolError, match='dev.sh'):
        tools_mod._sim('GET', '/units')


def test_facility_options_ranks_reachable_by_drive_time(rt, monkeypatch):
    times = {'h1': None, 'h2': 300}
    monkeypatch.setattr(rt, 'plan_route', lambda o, d: {'reachable': times[d['facility_id']] is not None,
                                                        'travel_time_s': times[d['facility_id']]}, raising=False)
    out = rt.facility_options(0, 0, 'hospital')
    assert [o['facility_id'] for o in out] == ['h2', 'h1'] and out[1]['reachable'] is False
    with pytest.raises(ToolError, match='types on this map'):
        rt.facility_options(0, 0, 'airport')


def test_assign_units_never_reuses_a_unit(rt, monkeypatch):
    fleet = [{'unit_id': 'u1', 'travel_time_s': 60}, {'unit_id': 'u2', 'travel_time_s': 90}]
    monkeypatch.setattr(rt, 'nearest_units', lambda incident_id, type, k: fleet, raising=False)
    out = rt.assign_units(['i1', 'i2', 'i3'], 'ambulance')
    assert [a['unit_id'] for a in out['assignments']] == ['u1', 'u2'] and out['unassigned'] == ['i3']


def test_upsert_decision_checks_units_before_writing(rt, monkeypatch):
    monkeypatch.setattr(rt, 'list_resources', lambda: [{'id': 'u1'}], raising=False)
    iid = '00000000-0000-0000-0000-0000000000a1'
    with pytest.raises(ToolError, match='not an incident id'):
        rt.upsert_decision('i1', [{'unit_id': 'u1'}])
    with pytest.raises(ToolError, match='list of'):
        rt.upsert_decision(iid, [{'role': 'transport'}])
    with pytest.raises(ToolError, match='unknown unit ids'):
        rt.upsert_decision(iid, [{'unit_id': 'u9'}])


def test_no_agent_can_dispatch():
    """A human approves every dispatch: /dispatch is not an agent tool."""
    assert 'dispatch' not in tools_mod.TOOL_SPECS and not hasattr(RunTools, 'dispatch')


def test_batches_run_at_the_same_time(monkeypatch):
    import threading, time
    running, peak, lock = [0], [0], threading.Lock()

    def slow(*a):
        with lock:
            running[0] += 1; peak[0] = max(peak[0], running[0])
        time.sleep(0.2)
        with lock:
            running[0] -= 1
        return {'output': {}, 'tool_calls': [], 'steps': 1}
    monkeypatch.setattr(runner, 'run_agent', slow)
    records = [{'observation_id': f'o{i}', 'sim_time_s': i} for i in range(6)]   # 3 batches of 2
    orch = queue_orchestrator(records)
    started = time.monotonic()
    assert orch.cycle() == 6
    assert peak[0] >= 4 and time.monotonic() - started < 0.5   # 3 batches x 2 agents overlapped, not 3 x 0.2 s in a row


def test_follow_switches_when_a_new_earthquake_is_generated(monkeypatch):
    runs = iter([None, 'run-A', 'run-A', 'run-B', 'stop'])
    made, cycled = [], []

    class Fake:
        def __init__(self, run_id, *a, **k):
            if run_id == 'stop':
                raise KeyboardInterrupt
            self.run_id = run_id
            made.append(run_id)
            self.tools = type('T', (), {'ingestion_status': staticmethod(lambda: {'ready': True})})()

        def cycle(self, chunk):
            cycled.append(self.run_id)
            return 1
    monkeypatch.setattr(orchestrator, 'current_run', lambda: next(runs))
    monkeypatch.setattr(orchestrator, 'Orchestrator', Fake)
    monkeypatch.setattr(orchestrator.time, 'sleep', lambda s: None)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.follow()
    assert made == ['run-A', 'run-B'] and cycled == ['run-A', 'run-A', 'run-B']
