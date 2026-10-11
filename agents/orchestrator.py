'''
Orchestrator: runs the agents in the order of the team architecture diagram. It decides WHICH agent runs
WHEN and passes each one's output to the next; it does no AI work itself.

    new reports ──► in_agent (each report, in parallel)
                ──► detect_agent ‖ medical_agent                  (same batch, in parallel)
                ──► assessment coordinator (merges their answers per incident; code, no AI)
                ──► response_agent                                (decides who goes where and dispatches)

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
import argparse, concurrent.futures, pathlib, threading, time
from agents.llm import run_agent
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


# The response agent runs after each assessment batch (and when units come free): it reads the incidents and
# the units, and dispatches. Resource and plan are one agent, with no drafts or approval; routing is code.
RESPONSE_AGENTS = {
    'response_agent': ('responseAgent/response-agent/response-agent.md',
                       ['dispatch_units']),   # one tool and few steps: each model turn is slow
}


class Orchestrator:
    _reading = False   # True while reports are read on their own thread (keep_reading)

    def __init__(self, run_id, instructions_override=None, batch_size=5, in_workers=12, parallel_batches=3):
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
        self.response = {}                        # the same, for the response agent
        for name, (path, tools) in RESPONSE_AGENTS.items():
            md = pathlib.Path((instructions_override or {}).get(name) or AGENTS_DIR / path)
            if md.exists():
                self.response[name] = (md.read_text(), tools)
        self._responded_to = None                 # the situation the response agent last acted on
        self._responding = threading.Lock()       # one response round at a time
        self._stop_responding = threading.Event()
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

    # ---- stage 1b: triage, by code ------------------------------------------
    # A big earthquake sends hundreds of reports in minutes; detect and medical take a minute or more for five.
    # A report that says people are trapped cannot wait for its turn. The in_agent has already read it (who,
    # where, which building), so code opens the incident at once and the response agent can send help within
    # seconds. Detect and medical still look at the report later and correct the incident.

    def triage(self, records):
        """Open or reinforce an incident for every record that reports trapped people. Returns what it did."""
        opened, reinforced = [], []
        for r in sorted(records, key=lambda r: r.get('sim_time_s') or 0):
            people, where = r.get('people') or {}, r.get('location') or {}
            point = where.get('point')
            if not (people.get('trapped') or people.get('trapped_mentioned')) or not point:
                continue
            try:
                candidates = where.get('building_candidates') or []
                building = next((c for c in candidates if c.get('fits_description')), candidates[0] if candidates else None)
                near = self.tools.find_nearby_incidents(point[0], point[1], TRIAGE_SAME_INCIDENT_M)
                trapped = people.get('trapped')
                if near:   # the same place is already known: one more report for it, and the larger count
                    i = near[0]
                    supports = list(dict.fromkeys(((i.get('evidence') or {}).get('supports') or []) + [r['observation_id']]))
                    groups = [dict(g) for g in i.get('person_groups') or []]
                    known = next((g for g in groups if g.get('condition') == 'trapped'), None)
                    if known is None:
                        groups.append(_trapped_group(trapped, r))
                    elif trapped and trapped > (known.get('est_count') or 0):
                        known['est_count'] = trapped
                    self.tools.upsert_incident('corroborated' if len(supports) > 1 and i['status'] == 'reported' else i['status'],
                                               location=i.get('location'), uncertainty_m=i.get('uncertainty_m'),
                                               building_id=i.get('building_id'), person_groups=groups,
                                               evidence={'supports': supports, 'contradicts': (i.get('evidence') or {}).get('contradicts') or []},
                                               incident_id=i['id'], sim_time_s=r.get('sim_time_s'))
                    reinforced.append(i['id'])
                else:
                    made = self.tools.upsert_incident('reported', location={'longitude': point[0], 'latitude': point[1]},
                                                      uncertainty_m=where.get('uncertainty_m'),
                                                      building_id=building['building_id'] if building else None,
                                                      person_groups=[_trapped_group(trapped, r)],
                                                      evidence={'supports': [r['observation_id']], 'contradicts': []},
                                                      sim_time_s=r.get('sim_time_s'))
                    opened.append(made['id'])
            except Exception as exc:   # one report that cannot be triaged must not stop the others
                print(f'  triage: skipped a report ({type(exc).__name__}: {str(exc)[:120]})', flush=True)
        if opened or reinforced:
            last = max(records, key=lambda r: r.get('sim_time_s') or 0)['observation_id']
            n = len(opened) + len(reinforced)
            self.tools.log_agent_run('triage', last, {
                'situation_summary': f'{n} report{"" if n == 1 else "s"} of people trapped: opened {len(opened)} '
                                     f'incident{"" if len(opened) == 1 else "s"}, added {len(reinforced)} to incidents already known.',
                'incidents': opened + sorted(set(reinforced))}, run_key=f'triage:{last}')
            print(f'  triage: {len(opened)} incidents opened, {len(reinforced)} reports added to known incidents', flush=True)
        return {'opened': opened, 'reinforced': reinforced}

    def keep_reading(self, interval_s=2, chunk=12):
        """Read new reports continuously on their own thread (in_agent, then triage), so a slow assessment
        batch never holds up the reading of the reports behind it. Small passes, so the first trapped people
        are triaged a few seconds after their reports arrive instead of after a long first read."""
        self._reading = True

        def loop():
            while not self._stop_responding.wait(interval_s):
                try:
                    reports = self.new_reports(chunk)
                    if reports:
                        print(f'{len(reports)} new reports -> in_agent', flush=True)
                        self.triage(self.normalize(reports))
                except Exception as exc:
                    print(f'  reading failed, trying again: {type(exc).__name__}: {str(exc)[:160]}', flush=True)
        threading.Thread(target=loop, name=f'read-{self.run_id[:8]}', daemon=True).start()

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

    # ---- stage 3: the response agent ---------------------------------------

    def respond(self):
        """Run the response agent if there is something it could do: an unresolved incident, a free unit,
        and a situation that has changed since it last looked. Returns its result, or None when skipped."""
        if not self.response or not self._responding.acquire(blocking=False):
            return None                            # a round is already under way; the next look catches up
        try:
            return self._respond()
        finally:
            self._responding.release()

    def keep_responding(self, interval_s=5):
        """Look for someone to send every few seconds, on its own thread, for as long as this run is worked on.
        Assessment can take minutes for a batch; an incident it has already written should not wait for the
        rest of the batch, and a unit that comes free should not wait either. A look costs no model time
        unless there is a new need and a free unit."""
        def loop():
            while not self._stop_responding.wait(interval_s):
                try:
                    self.respond()
                except Exception as exc:   # never let one bad look end the watching
                    print(f'  response look failed: {type(exc).__name__}: {exc}', flush=True)
        threading.Thread(target=loop, name=f'respond-{self.run_id[:8]}', daemon=True).start()

    def stop_responding(self):
        self._stop_responding.set()

    def _respond(self):
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

    # ---- the queue ------------------------------------------------------------
    # Two stages, both remembered in Neon (ops.agent_runs), so stopping, crashing or a model error never loses
    # work: a report waits for the in_agent until it has a successful in_agent record; an in_agent record waits
    # for assessment until a batch containing it completes (at least one assessment agent succeeded).

    def pending_assessment(self):
        """in_agent records not assessed yet, oldest first (also those left over from an earlier run of this)."""
        done = self.tools.assessed_observations()
        records = [r for r in self.tools.agent_outputs(IN_AGENT) if r.get('observation_id') not in done]
        # Most urgent first (the in_agent rates each report 1-5), then oldest: with hundreds of reports waiting,
        # the agents' time goes to the ones that matter.
        return sorted({r['observation_id']: r for r in records}.values(),
                      key=lambda r: (-(r.get('urgency') or 0), r.get('sim_time_s') or 0))

    def cycle(self, limit=None):
        """One pass: new reports -> in_agent, then every waiting in_agent record -> assessment, in batches.
        Returns how many items moved forward (0 = nothing to do)."""
        reports = [] if self._reading else self.new_reports(limit)   # read on their own thread when watching
        if reports:
            print(f'{len(reports)} new reports -> in_agent', flush=True)
            self.triage(self.normalize(reports))
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
        if assessed:
            self.respond()   # new incidents: who goes where
        return len(reports) + assessed

    def watch(self, interval_s=10, chunk=10):
        """Keep going while reports arrive. Each cycle takes at most `chunk` new reports, so the first incidents
        appear after one small batch instead of after the in_agent has read the whole backlog."""
        print(f'Watching for new reports every {interval_s}s, {chunk} at a time (Ctrl+C to stop)...', flush=True)
        self.keep_responding()   # the response agent does not wait for assessment batches
        self.keep_reading()      # and neither does the reading of new reports
        while True:
            if not self.cycle(chunk):
                time.sleep(interval_s)


TRIAGE_SAME_INCIDENT_M = 60   # a report this close to a known incident is about the same place


def _trapped_group(trapped, record):
    return {'group_id': 'grp_trapped', 'est_count': trapped or None, 'condition': 'trapped',
            'needs': ['extrication', 'medical'], 'confidence': record.get('source_reliability')}


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
            if orch is not None:
                orch.stop_responding()
            orch = candidate
            orch.keep_responding()   # the response agent does not wait for assessment batches
            orch.keep_reading()      # and neither does the reading of new reports
        if orch is None or not orch.cycle(chunk):
            time.sleep(interval_s)


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
