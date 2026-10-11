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
    orch.response, orch._responded_to, orch._respond_lock = {}, None, __import__('threading').Lock()   # no response agent here
    orch.medical_queue, orch._queue_lock = [], __import__('threading').Lock()
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
        return {'output': {'places': [], 'situation_summary': ''}, 'tool_calls': [], 'steps': 1}   # valid for both
    half.calls = []
    monkeypatch.setattr(runner, 'run_agent', half)
    orch = queue_orchestrator(RECORDS[:2])
    assert orch.cycle() == 2


# ---- response side (Gerard's tools are tested in test_response_tools.py) ----------------









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
    made, cycled, responding = [], [], []

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

        def keep_responding(self):      # the response agent's own loop starts with a run and stops when it is left
            responding.append(self.run_id)

        def stop_responding(self):
            responding.remove(self.run_id)

        def keep_reading(self):
            pass
    monkeypatch.setattr(orchestrator, 'current_run', lambda: next(runs))
    monkeypatch.setattr(orchestrator, 'Orchestrator', Fake)
    monkeypatch.setattr(orchestrator.time, 'sleep', lambda s: None)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.follow()
    assert made == ['run-A', 'run-B'] and cycled == ['run-A', 'run-A', 'run-B']
    assert responding == ['run-B']                      # only the run on screen is being responded to


def test_reports_of_trapped_people_open_an_incident_at_once_without_waiting_for_assessment():
    """Triage is code: the in_agent has already read who is trapped and where."""
    import threading
    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    incidents, logged = [], []

    class Tools:
        def find_nearby_incidents(self, lon, lat, radius_m):
            return [i for i in incidents if abs(i['location']['longitude'] - lon) < 0.0003]
        def upsert_incident(self, status, incident_id=None, **fields):
            row = next((i for i in incidents if i['id'] == incident_id), None)
            if row is None:
                row = {'id': f'inc-{len(incidents) + 1}'}
                incidents.append(row)
            row.update(status=status, **fields)
            return row
        def log_agent_run(self, *a, **k): logged.append(a)
    orch.tools = Tools()
    record = lambda obs, lon, trapped, **more: {
        'observation_id': obs, 'sim_time_s': 10, 'source_reliability': 0.9, 'urgency': 5,
        'people': {'trapped': trapped, 'trapped_mentioned': True, **more.pop('people', {})},
        'location': {'point': [lon, 35.69], 'uncertainty_m': 50,
                     'building_candidates': [{'building_id': 'b-far', 'fits_description': False}, {'building_id': 'b-fits', 'fits_description': True}]}, **more}
    out = orch.triage([record('o1', 139.700, 4), record('o2', 139.7001, 9), record('o3', 139.710, None),
                       {'observation_id': 'o4', 'people': {'injured': 3}, 'location': {'point': [139.72, 35.69]}},   # nobody trapped
                       {'observation_id': 'o5', 'people': {'trapped': 2, 'trapped_mentioned': True}, 'location': {}}])   # nowhere to send anyone
    assert len(out['opened']) == 2 and out['reinforced'] == ['inc-1'] and len(incidents) == 2
    first, second = incidents
    assert first['status'] == 'corroborated' and first['building_id'] == 'b-fits' and first['evidence']['supports'] == ['o1', 'o2']
    assert first['person_groups'][0]['condition'] == 'trapped' and first['person_groups'][0]['est_count'] == 9   # the larger count
    assert second['status'] == 'reported' and second['person_groups'][0]['est_count'] is None
    assert logged[0][0] == 'triage' and 'opened 2 incidents' in logged[0][2]['situation_summary']


def test_the_most_urgent_reports_are_assessed_first():
    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    orch.tools = type('T', (), {'assessed_observations': lambda self: {'done'}, 'agent_outputs': lambda self, agent: [
        {'observation_id': 'old-minor', 'urgency': 1, 'sim_time_s': 5}, {'observation_id': 'late-urgent', 'urgency': 5, 'sim_time_s': 90},
        {'observation_id': 'done', 'urgency': 5, 'sim_time_s': 1}, {'observation_id': 'early-urgent', 'urgency': 5, 'sim_time_s': 20}]})()
    assert [r['observation_id'] for r in orch.pending_assessment()] == ['early-urgent', 'late-urgent', 'old-minor']


# ---- human in the loop: the response agent proposes, the operator approves ---------------

class ProposalTools:
    run_id = 'run'

    def __init__(self, drafts=(), free=('F1', 'A1')):
        self.drafts, self.free, self.closed, self.proposed = list(drafts), set(free), [], []

    def _units(self):
        return [{'id': u, 'status': 'available' if u in self.free else 'en_route'} for u in ('F1', 'A1', 'P1')]

    def proposals(self, status='draft'):
        return self.drafts

    def close_proposal(self, d, status, reason=None):
        self.closed.append((d['id'], status, reason))

    def propose_dispatch(self, unit_id, incident_id, unit_type=None, eta_s=None):
        self.proposed.append((unit_id, incident_id, unit_type, eta_s))
        return {'id': f'd{len(self.proposed)}'}


def situation():
    return {'incidents': [{'incident_id': 'I1', 'could_use': ['fire_engine', 'ambulance'],
                           'nearest_free': {'fire_engine': [{'unit_id': 'F1', 'travel_time_s': 60}],
                                            'ambulance': [{'unit_id': 'A1', 'travel_time_s': 30}]}}],
            'free_units': {'fire_engine': 1, 'ambulance': 1},
            'suggested': [{'unit_id': 'F1', 'incident_id': 'I1'}, {'unit_id': 'A1', 'incident_id': 'I1'}]}


def test_pending_proposals_are_not_offered_again():
    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    orch.tools = ProposalTools(drafts=[{'id': 'd0', 'incident_id': 'I1', 'assignments': [{'resource_id': 'F1', 'type': 'fire_engine'}]}])
    out = orch._without_pending(situation())
    assert out['incidents'][0]['could_use'] == ['ambulance'] and 'fire_engine' not in out['incidents'][0]['nearest_free']
    assert out['suggested'] == [{'unit_id': 'A1', 'incident_id': 'I1'}] and orch.tools.closed == []


def test_stale_proposals_expire():
    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    orch.tools = ProposalTools(drafts=[{'id': 'd0', 'incident_id': 'I1', 'assignments': [{'resource_id': 'F1', 'type': 'fire_engine'}]},
                                       {'id': 'd9', 'incident_id': 'GONE', 'assignments': [{'resource_id': 'A1', 'type': 'ambulance'}]}],
                               free=('A1',))
    orch._without_pending(situation())
    assert orch.tools.closed == [('d0', 'invalidated', 'unit no longer free'), ('d9', 'invalidated', 'incident no longer needs a unit')]


def test_propose_mode_saves_drafts_instead_of_dispatching(monkeypatch):
    monkeypatch.setattr(orchestrator, 'RESPONSE_MODE', 'propose')
    tools = ProposalTools()
    tools.incidents_to_cover = lambda: situation()
    tools.toolbox = lambda name, names, done=None: ([], {'dispatch_units': lambda real: pytest.fail('must not dispatch')})
    tools.log_agent_run = lambda *a, **k: None

    def agent(instructions, payload, specs, functions, max_steps=6):
        out = functions['dispatch_units']([{'unit': 'F1', 'incident': 'I1'}, {'unit': 'A1', 'incident': 'I1'}])
        assert [s['unit'] for s in out['sent']] == ['F1', 'A1']
        return {'output': {'summary': 'proposed'}, 'tool_calls': [], 'steps': 2}
    monkeypatch.setattr(orchestrator, 'run_agent', agent)
    monkeypatch.setattr(orchestrator, '_brief', lambda s: ({}, {'F1': 'F1', 'A1': 'A1', 'I1': 'I1'}))
    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    orch.tools, orch.response, orch._responded_to, orch.cursor = tools, {'response_agent': ('md', ['dispatch_units'])}, None, 'o1'
    result = orch.respond()
    assert tools.proposed == [('F1', 'I1', 'fire_engine', 60), ('A1', 'I1', 'ambulance', 30)]
    assert result['output']['mode'] == 'propose' and len(result['output']['proposed']) == 2
