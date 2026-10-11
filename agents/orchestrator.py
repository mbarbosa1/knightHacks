'''
Orchestrator: runs the agents in the order of the team architecture diagram. It decides WHICH agent runs
WHEN and passes each one's output to the next; it does no AI work itself.

    new reports ──► in_agent (each report, in parallel)
                ──► detect_agent ‖ medical_agent ‖ verify_agent   (same batch, in parallel)
                ──► [assessment coordinator ──► response: resource ‖ route ──► plan ──► report]   (next)

Run it (from the repo root; ./dev.sh running; SSH tunnel to the AMD server open):
    .venv/bin/python -m agents.orchestrator <run_id>              # process every report not done yet, then stop
    .venv/bin/python -m agents.orchestrator <run_id> --watch      # keep checking for new reports (live loop)
    .venv/bin/python -m agents.orchestrator <run_id> --batch-size 5 --limit 10

An agent takes part once its instructions file exists (agents/<group>/<name>.md); until then it is skipped,
so teammates can add agents one at a time. Try an instructions file before committing it with
    --instructions detect_agent=/path/to/detect-agent.md

Batches: assessment agents get a few normalized reports at a time (default 5), oldest first, so each batch
sees the incidents the previous one created and the model's context stays small.

Every batch is logged to Neon as an ops.agent_runs row (agent 'orchestrator') with each agent's result,
tool calls and errors; the agents log their own runs too (log_agent_run in their .md).
'''
import argparse, concurrent.futures, pathlib, time
from agents.llm import run_agent
from agents.tools import RunTools
from agents.assesmentAgent.in_agent import InAgent, AGENT as IN_AGENT

AGENTS_DIR = pathlib.Path(__file__).resolve().parent

# Assessment agents that run in parallel on each batch: instructions file + the tools they may call.
ASSESSMENT_AGENTS = {
    'detect_agent': ('assesmentAgent/detect-agent.md',
                     ['get_run', 'list_observations', 'list_incidents', 'get_last_agent_run', 'find_nearby_incidents',
                      'find_nearby_buildings', 'find_nearest_road', 'upsert_incident', 'set_road_restriction',
                      'log_agent_run']),
    'medical_agent': ('assesmentAgent/medical-agent.md',
                      ['get_run', 'list_observations', 'list_incidents', 'get_last_agent_run', 'find_nearby_incidents',
                       'upsert_incident', 'log_agent_run']),
    'verify_agent': ('assesmentAgent/verify-agent.md',
                     ['get_run', 'list_observations', 'list_incidents', 'get_last_agent_run', 'find_nearby_incidents',
                      'find_nearby_buildings', 'upsert_incident', 'log_agent_run']),
}


class Orchestrator:
    def __init__(self, run_id, instructions_override=None, batch_size=5, in_workers=6):
        self.in_agent = InAgent.for_run(run_id)
        self.tools = self.in_agent.tools          # one RunTools (map loaded once) shared by every agent
        self.run_id = run_id
        self.batch_size = batch_size
        self.in_workers = in_workers
        self.cursor = None                        # last observation id handed to the assessment agents
        self.assessment = {}                      # name -> (instructions, tool names) for agents that exist
        for name, (path, tools) in ASSESSMENT_AGENTS.items():
            md = pathlib.Path((instructions_override or {}).get(name) or AGENTS_DIR / path)
            if md.exists():
                self.assessment[name] = (md.read_text(), tools)
        missing = sorted(set(ASSESSMENT_AGENTS) - set(self.assessment))
        print(f'Assessment agents: {sorted(self.assessment) or "none"}'
              + (f'; skipped (no .md yet): {missing}' if missing else ''), flush=True)

    # ---- stage 1: in_agent on every new report ------------------------------

    def new_reports(self, limit=None):
        done = self.tools.processed_inputs(IN_AGENT)
        todo = [o for o in sorted(self.tools.list_observations(), key=lambda o: o['sim_time_s']) if o['id'] not in done]
        return todo[:limit] if limit else todo

    def normalize(self, reports):
        with concurrent.futures.ThreadPoolExecutor(self.in_workers) as pool:
            records = list(pool.map(self.in_agent.process, reports))
        failed = [r for r in records if r['error']]
        if failed:
            print(f'  in_agent: {len(failed)} failed (retried next cycle): {failed[0]["error"][:100]}', flush=True)
        return [r for r in records if not r['error']]

    # ---- stage 2: detect ‖ medical ‖ verify on one batch ---------------------

    def assess(self, batch):
        """Run every assessment agent on the same batch in parallel; returns name -> result or error."""
        payload = {'run_id': self.run_id, 'observation_ids': [r['observation_id'] for r in batch],
                   'cursor': self.cursor, 'normalized': batch}

        def one(name):
            instructions, tool_names = self.assessment[name]
            specs, functions = self.tools.toolbox(name, tool_names)
            started = time.monotonic()
            try:
                result = run_agent(instructions, payload, specs, functions)
                return name, {**result, 'seconds': round(time.monotonic() - started, 1)}
            except Exception as exc:   # one agent failing must not stop the others
                return name, {'error': f'{type(exc).__name__}: {exc}', 'seconds': round(time.monotonic() - started, 1)}

        with concurrent.futures.ThreadPoolExecutor(max(1, len(self.assessment))) as pool:
            results = dict(pool.map(one, self.assessment))
        self.cursor = batch[-1]['observation_id']
        return results

    # ---- one pass over everything new ---------------------------------------

    def cycle(self, limit=None):
        reports = self.new_reports(limit)
        if not reports:
            return 0
        print(f'{len(reports)} new reports -> in_agent', flush=True)
        records = self.normalize(reports)
        if not self.assessment:
            return len(reports)
        for i in range(0, len(records), self.batch_size):
            batch = records[i:i + self.batch_size]
            results = self.assess(batch)
            summary = {name: _summary(r) for name, r in results.items()}
            self.tools.log_agent_run('orchestrator', self.cursor,
                                     {'stage': 'assessment', 'observation_ids': [r['observation_id'] for r in batch],
                                      'agents': summary}, run_key=f'assessment:{self.cursor}')
            for name, s in summary.items():
                print(f'  batch {i // self.batch_size + 1}: {name:<14} {s}', flush=True)
        return len(reports)

    def watch(self, interval_s=10):
        print(f'Watching for new reports every {interval_s}s (Ctrl+C to stop)...', flush=True)
        while True:
            if not self.cycle():
                time.sleep(interval_s)


def _summary(result):
    """What the orchestrator records about one agent's run: enough to debug, not the whole conversation."""
    if 'error' in result and 'output' not in result:
        return {'error': result['error'], 'seconds': result['seconds']}
    out = result['output']
    writes = [c for c in result['tool_calls'] if c['tool'] in ('upsert_incident', 'set_road_restriction')]
    return {'seconds': result['seconds'], 'steps': result['steps'], 'tool_calls': len(result['tool_calls']),
            'writes_ok': sum(c['ok'] for c in writes), 'tool_errors': [c['error'] for c in result['tool_calls'] if not c['ok']][:5],
            'incidents': len(out.get('incidents') or []) if isinstance(out, dict) else None,
            'summary': (out.get('situation_summary') or '')[:300] if isinstance(out, dict) else None}


def main():
    ap = argparse.ArgumentParser(description='Run the agent pipeline on one simulation run.')
    ap.add_argument('run_id')
    ap.add_argument('--watch', action='store_true', help='keep checking for new reports')
    ap.add_argument('--batch-size', type=int, default=5)
    ap.add_argument('--limit', type=int, help='only take this many new reports (for testing)')
    ap.add_argument('--instructions', action='append', default=[], metavar='AGENT=PATH',
                    help='use this .md for an agent instead of agents/<group>/<name>.md')
    a = ap.parse_args()
    override = dict(x.split('=', 1) for x in a.instructions)
    print('Loading the run and its map...', flush=True)
    orch = Orchestrator(a.run_id, override, a.batch_size)
    status = orch.tools.ingestion_status()
    if not status.get('ready'):
        raise SystemExit(f'Run {a.run_id} is not ready for agents yet: {status}')
    if a.watch:
        orch.watch()
    else:
        print(f'Done: {orch.cycle(a.limit)} reports processed.')


if __name__ == '__main__':
    main()
