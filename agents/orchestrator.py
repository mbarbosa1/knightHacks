'''
Orchestrator: runs the agents in the order of the team architecture diagram. It decides WHICH agent runs
WHEN and passes each one's output to the next; it does no AI work itself.

    new reports ──► in_agent (each report, in parallel)
                ──► detect_agent ‖ medical_agent                  (same batch, in parallel)
                ──► response_agent                                (decides who goes where and dispatches)
                ──► [assessment coordination ──► response: resource ‖ route ──► plan]   (next)

Run it (from the repo root; ./dev.sh running; SSH tunnel to the AMD server open):
    .venv/bin/python -m agents.orchestrator <run_id>              # process every report not done yet, then stop
    .venv/bin/python -m agents.orchestrator <run_id> --watch      # keep checking for new reports (live loop)
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
from agents.llm import run_agent
from agents.tools import RunTools
from agents.loader import load_agent

_in_agent = load_agent('assesmentAgent/in-agent/in_agent.py')
InAgent, IN_AGENT = _in_agent.InAgent, _in_agent.AGENT

AGENTS_DIR = pathlib.Path(__file__).resolve().parent

# Assessment agents that run in parallel on each batch: instructions file + the tools they may call.
ASSESSMENT_AGENTS = {
    'detect_agent': ('assesmentAgent/detect-agent/detect-agent.md',
                     ['get_run', 'list_observations', 'list_incidents', 'get_last_agent_run', 'find_nearby_incidents',
                      'find_nearby_buildings', 'find_nearest_road', 'upsert_incident', 'set_road_restriction',
                      'log_agent_run']),
    'medical_agent': ('assesmentAgent/medical-agent/medical-agent.md',
                      ['get_run', 'list_observations', 'list_incidents', 'get_last_agent_run', 'find_nearby_incidents',
                       'upsert_incident', 'log_agent_run']),
}


# The response agent runs after each assessment batch (and when units come free): it reads the incidents and
# the units, and dispatches. Resource and plan are one agent, with no drafts or approval; routing is code.
RESPONSE_AGENTS = {
    'response_agent': ('responseAgent/response-agent/response-agent.md',
                       ['dispatch_units']),   # one tool and few steps: each model turn is slow
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
        self.response = {}                        # the same, for the response agent
        for name, (path, tools) in RESPONSE_AGENTS.items():
            md = pathlib.Path((instructions_override or {}).get(name) or AGENTS_DIR / path)
            if md.exists():
                self.response[name] = (md.read_text(), tools)
        self._responded_to = None                 # the situation the response agent last acted on
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
            done = {}                                   # filled by the agent's log_agent_run(input_ref) call
            specs, functions = self.tools.toolbox(name, tool_names, done)
            started = time.monotonic()
            try:
                result = run_agent(instructions, payload, specs, functions)
                output = result['output'] if isinstance(result['output'], dict) and result['output'] else done.get('output')
                result = {**result, 'output': output, 'seconds': round(time.monotonic() - started, 1)}
            except Exception as exc:   # one agent failing must not stop the others
                result = {'error': f'{type(exc).__name__}: {exc}', 'seconds': round(time.monotonic() - started, 1)}
            # The agent's audit entry, written by code (also when it failed): ops.agent_runs + outbox row.
            self.tools.log_agent_run(name, done.get('input_ref') or batch[-1]['observation_id'],
                                     result.get('output') or {'error': result.get('error')},
                                     int(result['seconds'] * 1000))
            return name, result

        with concurrent.futures.ThreadPoolExecutor(max(1, len(self.assessment))) as pool:
            results = dict(pool.map(one, self.assessment))
        self.cursor = batch[-1]['observation_id']
        return results

    # ---- stage 3: the response agent ---------------------------------------

    def respond(self):
        """Run the response agent if there is something it could do: an unresolved incident, a free unit,
        and a situation that has changed since it last looked. Returns its result, or None when skipped."""
        if not self.response:
            return None
        try:
            situation = self.tools.incidents_to_cover()
        except Exception as exc:   # no live run in the simulator, or it is not running: nothing to dispatch
            print(f'  response skipped: {exc}', flush=True)
            return None
        # Code checks there is something to decide before spending model time on it: an incident that could
        # use a unit, a free unit that can reach it, and a situation that changed since the agent last looked.
        if not any(units for i in situation['incidents'] for units in i.get('nearest_free', {}).values()) \
                or _needs(situation) == self._responded_to:
            return None
        payload, ids = _brief(situation)
        name = 'response_agent'
        instructions, tool_names = self.response[name]
        specs, functions = self.tools.toolbox(name, tool_names)
        sent, refused = [], []                     # what really happened, whatever the agent says afterwards
        dispatch = functions['dispatch_units']
        refs = {full: short for short, full in ids.items()}

        def dispatch_units(assignments):
            real = [{'unit_id': ids.get(str((a or {}).get('unit')).upper(), (a or {}).get('unit')),
                     'incident_id': ids.get(str((a or {}).get('incident')).upper(), (a or {}).get('incident'))}
                    for a in assignments or []]
            outcome = dispatch(real)
            sent.extend(outcome['sent'])
            refused.extend(outcome['refused'])
            short = lambda r: {'unit': refs.get(r['unit_id'], r['unit_id']), 'incident': refs.get(r['incident_id'], r['incident_id'])}
            return {'sent': [{**short(r), 'eta_s': r['eta_s']} for r in outcome['sent']],
                    'refused': [{**short(r), 'reason': r['reason']} for r in outcome['refused']]}
        functions['dispatch_units'] = dispatch_units
        started = time.monotonic()
        try:
            result = run_agent(instructions, payload, specs, functions, max_steps=6)
            result = {**result, 'output': result['output'] if isinstance(result['output'], dict) else {}}
        except Exception as exc:
            result = {'error': f'{type(exc).__name__}: {exc}', 'output': {}, 'tool_calls': [], 'steps': None}
        result['seconds'] = round(time.monotonic() - started, 1)
        result['output'] = {**result['output'], 'dispatched': sent, 'refused': refused,
                            **({'error': result['error']} if 'error' in result else {})}
        self.tools.log_agent_run(name, self.cursor or 'no-reports-yet', result['output'], int(result['seconds'] * 1000))
        try:   # what it left behind, so an unchanged situation is not handed to it again
            self._responded_to = _needs(self.tools.incidents_to_cover())
        except Exception:
            self._responded_to = None
        print(f'  response: {_response_summary(result)}', flush=True)
        return result

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
            self.respond()
        return len(reports)

    def watch(self, interval_s=10):
        print(f'Watching for new reports every {interval_s}s (Ctrl+C to stop)...', flush=True)
        while True:
            if not self.cycle():
                self.respond()   # no new reports, but a unit may have come free or reported from the scene
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


def _needs(situation):
    """What in a situation could change the response agent's mind: which incidents could use which kinds of
    unit, who could go, and how many units are free. A unit going from en route to on scene changes none
    of that, so it does not wake the agent."""
    return ([(i['incident_id'], i['could_use'], [[u['unit_id'] for u in units] for units in i.get('nearest_free', {}).values()])
             for i in situation['incidents']], situation['free_units'])


UNIT_LETTER = {'fire_engine': 'F', 'ambulance': 'A', 'police': 'P'}


def _brief(situation):
    """The situation as the response agent reads it, with short names in place of ids (I1, F2, A1...): the
    model is slow, and most of a turn went on writing 36-character ids. Returns (payload, short name -> id)."""
    ids, short = {}, {}

    def ref(full, prefix):
        if full not in short:
            short[full] = f'{prefix}{sum(1 for s in short.values() if s.startswith(prefix)) + 1}'
            ids[short[full]] = full
        return short[full]

    incidents = []
    for i in situation['incidents']:
        incidents.append({
            'incident': ref(i['incident_id'], 'I'), 'status': i['status'], 'people': i['people'],
            'units_on_it': [u['type'] for u in i['units_on_it']],
            'field_reports': [{k: v for k, v in r.items() if k != 'sim_time_s'} for r in i['field_reports']],
            'could_use': i['could_use'],
            'nearest_free': {t: [{'unit': ref(u['unit_id'], UNIT_LETTER[t]), 'travel_time_s': u['travel_time_s']} for u in units]
                             for t, units in i['nearest_free'].items()}})
    suggested = [{'unit': short[s['unit_id']], 'incident': short[s['incident_id']]} for s in situation['suggested']]
    return {'incidents': incidents, 'free_units': situation['free_units'], 'suggested': suggested}, ids


def _response_summary(result):
    out = result['output']
    return {'seconds': result['seconds'], 'steps': result['steps'], 'dispatched': len(out['dispatched']),
            'refused': [r['reason'] for r in out['refused']][:5],
            'tool_errors': [c['error'] for c in result['tool_calls'] if not c['ok']][:5],
            'not_covered': len(out.get('not_covered') or []), 'summary': (out.get('summary') or '')[:300],
            **({'error': out['error']} if 'error' in out else {})}


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
