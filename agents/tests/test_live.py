"""End to end on a real, live run: the model on the AMD server (tunnel open), ./dev.sh, Neon.
Skipped unless AGENTS_LIVE_RUN=<run_id> is set:

    AGENT_LLM=on AGENTS_LIVE_RUN=<run_id> .venv/bin/python -m pytest agents/tests -m live -s
"""
import os
import pytest
from agents import orchestrator

RUN_ID = os.environ.get('AGENTS_LIVE_RUN')
pytestmark = [pytest.mark.live, pytest.mark.skipif(not RUN_ID, reason='set AGENTS_LIVE_RUN=<run_id> to run')]


@pytest.fixture(scope='module')
def agent():
    return orchestrator.InAgent.for_run(RUN_ID)


def test_in_agent_reads_real_reports(agent):
    reports = agent.tools.list_observations()[:3]
    assert reports, 'the run has no released reports yet'
    for report in reports:
        record = agent.normalize(report)
        assert record['error'] is None, record['error']
        assert record['event_type'] in orchestrator._in_agent.EVENT_TYPES and record['summary_en']
        for candidate in record['location']['building_candidates']:
            assert candidate['building_id'] in agent.tools.map.buildings   # real map ids only


def test_assessment_agents_run_on_one_report(agent):
    orch = orchestrator.Orchestrator(RUN_ID, batch_size=1)
    report = agent.tools.list_observations()[0]
    batch = [agent.normalize(report)]
    for name, result in orch.assess(batch).items():
        assert 'error' not in result, f'{name}: {result.get("error")}'
        saved = orch.tools.get_last_agent_run(name)
        assert saved and saved['id'] == result['audit_id'], f'{name} was not saved by the orchestrator'
        assert saved['output'] == result['output']
        assert saved['input_ref'] == report['id']
