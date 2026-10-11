'''
Medical agent: estimates who needs help and how badly. Updates each incident's person_groups (count,
condition, needs, triage) and says how many ambulances / extrications are needed. Instructions: medical-agent.md.

It is an instructions file + tools: the model decides which tool to call, agents/tools.py runs it.
The orchestrator runs it in parallel with the detect agent on each batch of in_agent records.

Run it alone (repo root; ./dev.sh running; SSH tunnel open; in_agent records already in Neon):
    .venv/bin/python agents/assesmentAgent/medical-agent/medical_agent.py <run_id> [--limit 3]
'''
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))   # repo root, when run as a script
from agents.runner import agent_main, run_md_agent  # noqa: E402

AGENT = 'medical_agent'
INSTRUCTIONS = pathlib.Path(__file__).with_name('medical-agent.md')
# The tools named in medical-agent.md; agents/tools.py implements them.
TOOLS = ['get_run', 'list_observations', 'list_incidents', 'get_last_agent_run', 'find_nearby_incidents',
         'upsert_incident', 'log_agent_run']


def run(tools, batch, cursor=None, instructions=None):
    """One invocation on a batch of in_agent records -> run_md_agent's result (also logged to ops.agent_runs)."""
    return run_md_agent(tools, AGENT, instructions or INSTRUCTIONS.read_text(), TOOLS, batch, cursor)


if __name__ == '__main__':
    agent_main(AGENT, run)
