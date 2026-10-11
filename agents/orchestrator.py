'''
Orchestrator: runs the agents in the order of the team architecture diagram. It decides WHICH agent runs
WHEN and passes each one's output to the next; it does no AI work itself.

    new reports ──► in_agent (each report, in parallel)
                ──► detect_agent ‖ medical_agent                  (same batch, in parallel)
                ──► assessment coordinator (merges their answers per incident; code, no AI)
                ──► [response: resource ‖ route ──► plan]   (next)

Each agent lives in its own folder with its instructions and code, e.g. assesmentAgent/detect-agent/
(detect-agent.md + detect_agent.py, which lists its tools). agents/runner.py runs a .md + tools agent.

Run it (from the repo root; ./dev.sh running; SSH tunnel to the AMD server open):
    .venv/bin/python -m agents.orchestrator <run_id>              # process every report not done yet, then stop
    .venv/bin/python -m agents.orchestrator <run_id> --watch      # keep checking for new reports (live loop)
    .venv/bin/python -m agents.orchestrator --follow              # the run the website shows; switches on Generate
    .venv/bin/python -m agents.orchestrator <run_id> --batch-size 5 --limit 10

An agent takes part once its instructions file exists (agents/<group>/<name>/<name>.md, e.g.
assesmentAgent/detect-agent/detect-agent.md); until then it is skipped,
so teammates can add agents one at a time. Try an instructions file before committing it with
    --instructions detect_agent=/path/to/detect-agent.md

Batches: assessment agents get a few normalized reports at a time (default 5), oldest first, so each batch
sees the incidents the previous one created and the model's context stays small.

Every batch is logged to Neon as an ops.agent_runs row (agent 'orchestrator') with each agent's result,
tool calls and errors. Each agent's own entry (agent = its name, output = its final answer, or its error) is
written here too: the agent's log_agent_run(input_ref) call only tells us which report it got to.
'''
import argparse, concurrent.futures, pathlib, time
from agents.tools import RunTools
from agents.loader import load_agent

_in_agent = load_agent('assesmentAgent/in-agent/in_agent.py')
InAgent, IN_AGENT = _in_agent.InAgent, _in_agent.AGENT
coordinator = load_agent('assesmentAgent/assessment-coordinator/assessment_coordinator.py')

AGENTS_DIR = pathlib.Path(__file__).resolve().parent

# Assessment agents that run in parallel on each batch. Each agent's file (AGENT, INSTRUCTIONS, TOOLS, run)
# is the one place its tool list is kept.
ASSESSMENT_MODULES = {m.AGENT: m for m in (load_agent('assesmentAgent/detect-agent/detect_agent.py'),
                                           load_agent('assesmentAgent/medical-agent/medical_agent.py'))}
ASSESSMENT_AGENTS = {name: (str(m.INSTRUCTIONS.relative_to(AGENTS_DIR)), m.TOOLS) for name, m in ASSESSMENT_MODULES.items()}


class Orchestrator:
    def __init__(self, run_id, instructions_override=None, batch_size=5, in_workers=6, parallel_batches=3):
        self.in_agent = InAgent.for_run(run_id)
        self.tools = self.in_agent.tools          # one RunTools (map loaded once) shared by every agent
        self.run_id = run_id
        self.batch_size = batch_size
        self.in_workers = in_workers
        self.parallel_batches = parallel_batches   # assessment batches in flight at once (the model server batches requests)
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

    def assess(self, batch, cursor=None):
        """Run every assessment agent on the same batch in parallel; returns name -> result or error.
        Each agent's run() also writes its ops.agent_runs entry (its answer, or its error)."""
        def one(name):
            instructions, _ = self.assessment[name]
            return name, ASSESSMENT_MODULES[name].run(self.tools, batch, cursor, instructions)

        with concurrent.futures.ThreadPoolExecutor(max(1, len(self.assessment))) as pool:
            results = dict(pool.map(one, self.assessment))
        self.cursor = batch[-1]['observation_id']
        return results

    def assess_and_log(self, batch, number):
        """assess() one batch, log it, hand it to the coordinator. True when it completed (else it stays queued).
        Batches run in parallel, so everything here is per batch (no shared cursor)."""
        last = batch[-1]['observation_id']
        results = self.assess(batch)
        complete = any('error' not in r for r in results.values())
        summary = {name: _summary(r) for name, r in results.items()}
        self.tools.log_agent_run('orchestrator', last,
                                 {'stage': 'assessment', 'complete': complete,
                                  'observation_ids': [r['observation_id'] for r in batch], 'agents': summary},
                                 run_key=f'assessment:{last}')
        for name, s in summary.items():
            print(f'  batch {number}: {name:<14} {s}', flush=True)
        if not complete:
            print(f'  batch {number}: every agent failed; it stays in the queue', flush=True)
            return False
        handoff = coordinator.merge(results, batch)   # -> the response stage (next)
        self.tools.log_agent_run('assessment_coordinator', last, handoff, run_key=f'coordinator:{last}')
        print(f'  batch {number}: coordinator    {len(handoff["incidents"])} incidents, '
              f'{len(handoff["needs_response"])} need a response', flush=True)
        return True

    # ---- one pass over everything new ---------------------------------------

    # ---- the queue ------------------------------------------------------------
    # Two stages, both remembered in Neon (ops.agent_runs), so stopping, crashing or a model error never loses
    # work: a report waits for the in_agent until it has a successful in_agent record; an in_agent record waits
    # for assessment until a batch containing it completes (at least one assessment agent succeeded).

    def pending_assessment(self):
        """in_agent records not assessed yet, oldest first (also those left over from an earlier run of this)."""
        done = self.tools.assessed_observations()
        records = [r for r in self.tools.agent_outputs(IN_AGENT) if r.get('observation_id') not in done]
        return sorted({r['observation_id']: r for r in records}.values(), key=lambda r: r.get('sim_time_s') or 0)

    def cycle(self, limit=None):
        """One pass: new reports -> in_agent, then every waiting in_agent record -> assessment, in batches.
        Returns how many items moved forward (0 = nothing to do)."""
        reports = self.new_reports(limit)
        if reports:
            print(f'{len(reports)} new reports -> in_agent', flush=True)
            self.normalize(reports)
        if not self.assessment:
            return len(reports)
        pending = self.pending_assessment()
        pending = pending[:limit] if limit else pending
        if pending:
            print(f'{len(pending)} records waiting for assessment', flush=True)
        batches = [pending[i:i + self.batch_size] for i in range(0, len(pending), self.batch_size)]
        with concurrent.futures.ThreadPoolExecutor(max(1, self.parallel_batches)) as pool:
            done = list(pool.map(self.assess_and_log, batches, range(1, len(batches) + 1)))
        assessed = sum(len(b) for b, ok in zip(batches, done) if ok)
        return len(reports) + assessed

    def watch(self, interval_s=10, chunk=10):
        """Keep going while reports arrive. Each cycle takes at most `chunk` new reports, so the first incidents
        appear after one small batch instead of after the in_agent has read the whole backlog."""
        print(f'Watching for new reports every {interval_s}s, {chunk} at a time (Ctrl+C to stop)...', flush=True)
        while True:
            if not self.cycle(chunk):
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


def current_run():
    """The run the simulator is playing, i.e. the earthquake the website shows (None when nothing is live)."""
    import httpx
    from agents.tools import SIM_URL
    try:
        return httpx.get(f'{SIM_URL}/clock', timeout=10).json().get('run_id')
    except (httpx.HTTPError, ValueError):
        return None


def follow(override=None, batch_size=5, parallel=3, chunk=10, interval_s=10):
    """Work on whichever earthquake the website is showing, and switch by itself when someone clicks Generate."""
    orch = None
    print('Following the simulator: agents work on the earthquake the website shows (Ctrl+C to stop)...', flush=True)
    while True:
        run_id = current_run()
        if run_id and (orch is None or orch.run_id != run_id):
            print(f'\n== Now on run {run_id}. Loading its map...', flush=True)
            candidate = Orchestrator(run_id, override, batch_size, parallel_batches=parallel)
            if not candidate.tools.ingestion_status().get('ready'):
                print('   not ready for agents yet; checking again shortly', flush=True)
                time.sleep(interval_s)
                continue
            orch = candidate
        if orch is None or not orch.cycle(chunk):
            time.sleep(interval_s)


def main():
    ap = argparse.ArgumentParser(description='Run the agent pipeline on one simulation run.')
    ap.add_argument('run_id', nargs='?', help='the run to work on (omit with --follow)')
    ap.add_argument('--follow', action='store_true',
                    help='work on the run the website shows, switching when a new earthquake is generated')
    ap.add_argument('--watch', action='store_true', help='keep checking for new reports')
    ap.add_argument('--batch-size', type=int, default=5)
    ap.add_argument('--parallel', type=int, default=3, help='assessment batches at the same time (default 3)')
    ap.add_argument('--limit', type=int, help='only take this many new reports (with --watch: per cycle, default 10)')
    ap.add_argument('--instructions', action='append', default=[], metavar='AGENT=PATH',
                    help='use this .md for an agent instead of agents/<group>/<name>.md')
    a = ap.parse_args()
    override = dict(x.split('=', 1) for x in a.instructions)
    if a.follow:
        return follow(override, a.batch_size, a.parallel, a.limit or 10)
    if not a.run_id:
        ap.error('give a run_id, or use --follow')
    print('Loading the run and its map...', flush=True)
    orch = Orchestrator(a.run_id, override, a.batch_size, parallel_batches=a.parallel)
    status = orch.tools.ingestion_status()
    if not status.get('ready'):
        raise SystemExit(f'Run {a.run_id} is not ready for agents yet: {status}')
    if a.watch:
        orch.watch(chunk=a.limit or 10)
    else:
        print(f'Done: {orch.cycle(a.limit)} reports processed.')


if __name__ == '__main__':
    main()
