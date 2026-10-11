'''
Runs one tool-using agent (an instructions .md + a list of tools) on a batch of in_agent records, and the
small command line every agent file shares. Used by detect_agent.py, medical_agent.py and the orchestrator.

    result = run_md_agent(tools, 'detect_agent', instructions, tool_names, batch)
    -> {"output": <final JSON answer>, "tool_calls": [...], "steps": n, "seconds": s}   or {"error": ..., "seconds": s}

Every call writes the agent's ops.agent_runs entry (its final answer, or its error), so a failed agent is
visible in the log too. The agent's own log_agent_run(input_ref) tool call only says which report it got to.
'''
import argparse, time
from agents.llm import run_agent


def run_md_agent(tools, name, instructions, tool_names, batch, cursor=None):
    payload = {'run_id': tools.run_id, 'observation_ids': [r['observation_id'] for r in batch],
               'cursor': cursor, 'normalized': batch}
    done = {}                                   # filled by the agent's log_agent_run(input_ref) call
    specs, functions = tools.toolbox(name, tool_names, done)
    started = time.monotonic()
    try:
        result = run_agent(instructions, payload, specs, functions)
        output = result['output'] if isinstance(result['output'], dict) and result['output'] else done.get('output')
        result = {**result, 'output': output, 'seconds': round(time.monotonic() - started, 1)}
    except Exception as exc:   # reported, not raised: one agent failing must not stop the others
        result = {'error': f'{type(exc).__name__}: {exc}', 'seconds': round(time.monotonic() - started, 1)}
    tools.log_agent_run(name, done.get('input_ref') or (batch[-1]['observation_id'] if batch else None),
                        result.get('output') or {'error': result.get('error')}, int(result['seconds'] * 1000))
    return result


def agent_main(name, run):
    """Command line for one agent file: run it alone on the run's latest in_agent records.
        .venv/bin/python agents/assesmentAgent/<name>-agent/<name>_agent.py <run_id> [--limit 3]"""
    from agents.tools import RunTools
    ap = argparse.ArgumentParser(description=f'Run {name} alone on in_agent records already in Neon.')
    ap.add_argument('run_id')
    ap.add_argument('--limit', type=int, default=3, help='how many of the latest in_agent records to use')
    a = ap.parse_args()
    print('Loading the run and its map...', flush=True)
    tools = RunTools.for_run(a.run_id)
    batch = tools.agent_outputs('in_agent')[-a.limit:]
    if not batch:
        raise SystemExit('No in_agent records for this run yet: run the in agent (or the orchestrator) first.')
    print(f'{name} on {len(batch)} report(s)...', flush=True)
    result = run(tools, batch)
    if 'error' in result:
        raise SystemExit(f'{name} failed after {result["seconds"]}s: {result["error"]}')
    out = result['output'] or {}
    print(f'Done in {result["seconds"]}s, {result["steps"]} steps, {len(result["tool_calls"])} tool calls '
          f'({sum(not c["ok"] for c in result["tool_calls"])} rejected).')
    print('Summary:', out.get('situation_summary'))
    for inc in out.get('incidents') or []:
        print('  -', inc.get('incident_id'), inc.get('change'), inc.get('status') or '',
              inc.get('priority') or inc.get('medical_priority') or '')
