'''
Orchestrator: runs the agents in the order of the team architecture diagram. It decides WHICH agent runs
WHEN and passes each one's output to the next; it does no AI work itself.

    new reports ──► in_agent (each report, in parallel)
                ──► detect_agent ‖ medical_agent                  (same batch, in parallel)
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
import argparse, concurrent.futures, math, pathlib, time
from agents.llm import run_agent
from agents.tools import RunTools
from agents.loader import load_agent
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

_in_agent = load_agent('assesmentAgent/in-agent/in_agent.py')
InAgent, IN_AGENT = _in_agent.InAgent, _in_agent.AGENT

AGENTS_DIR = pathlib.Path(__file__).resolve().parent

# Assessment agents that run in parallel on each batch: instructions file + the tools they may call.
ASSESSMENT_AGENTS = {
    'detect_agent': ('assesmentAgent/detect-agent/detect-agent.md',
                     ['get_run', 'list_observations', 'list_incidents', 'get_last_agent_run', 'find_nearby_incidents',
                      'find_nearby_buildings', 'find_nearest_road', 'upsert_incident', 'set_road_restriction',
                      'log_agent_run']),
    'medical_agent': ('assesmentAgent/medical-agent/medical-agent.md', ['list_observations']),   # writes nothing
}


class MedicalLocation(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)

    @field_validator('longitude', 'latitude', mode='before')
    @classmethod
    def reject_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError('coordinates must be numbers, not booleans')
        return value


class MedicalPlace(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = None
    building_id: str | None = None
    location: MedicalLocation | None = None
    trapped: StrictInt | None = Field(default=None, ge=0, le=2**31 - 1)
    critical: StrictInt | None = Field(default=None, ge=0, le=2**31 - 1)
    injured: StrictInt | None = Field(default=None, ge=0, le=2**31 - 1)
    priority: StrictInt = Field(ge=1, le=3)
    why: str = ''
    observation_ids: list[str] = Field(min_length=1)

    @field_validator('location', mode='before')
    @classmethod
    def no_point_is_no_location(cls, value):
        """A vague report has no point; the model writes that as {longitude: null, latitude: null}."""
        if isinstance(value, dict) and value.get('longitude') is None and value.get('latitude') is None:
            return None
        return value

    @field_validator('trapped', 'critical', 'injured', 'priority', mode='before')
    @classmethod
    def parse_integer_string(cls, value):
        if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
            return int(value.strip())
        return value


class MedicalOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    situation_summary: str = ''
    places: list[MedicalPlace]


def validate_medical_output(output, batch):
    """Validate the entire result before logging success or changing the queue."""
    result = MedicalOutput.model_validate(output).model_dump()
    by_id = {record['observation_id']: record for record in batch}
    for place in result['places']:
        if any(source not in by_id for source in place['observation_ids']):
            raise ValueError('medical observation_ids must come from the current batch')
        candidates = {
            candidate['building_id']
            for source in place['observation_ids']
            for candidate in (by_id[source].get('location') or {}).get('building_candidates', [])
            if (by_id[source].get('location') or {}).get('precision') in ('gps', 'address')
        }
        if place['building_id'] not in candidates:
            place['building_id'] = None
    return result


class Orchestrator:
    def __init__(self, run_id, instructions_override=None, batch_size=5, in_workers=6):
        self.in_agent = InAgent.for_run(run_id)
        self.tools = self.in_agent.tools          # one RunTools (map loaded once) shared by every agent
        self.run_id = run_id
        self.batch_size = batch_size
        self.in_workers = in_workers
        self.cursor = None                        # last observation id handed to the assessment agents
        self.medical_queue = []                   # places with hurt people, from medical_agent; local only, never saved
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
            done = {}                                   # filled by the agent's log_agent_run(input_ref) call
            specs, functions = self.tools.toolbox(name, tool_names, done)
            started = time.monotonic()
            try:
                result = run_agent(instructions, payload, specs, functions)
                output = result['output'] if isinstance(result['output'], dict) and result['output'] else done.get('output')
                if name == 'medical_agent':
                    output = validate_medical_output(output, batch)
                result = {**result, 'output': output, 'seconds': round(time.monotonic() - started, 1)}
            except Exception as exc:   # one agent failing must not stop the others
                result = {'error': f'{type(exc).__name__}: {exc}', 'seconds': round(time.monotonic() - started, 1)}
            # The agent's audit entry, written by code (also when it failed): ops.agent_runs + outbox row.
            audit = self.tools.log_agent_run(name, done.get('input_ref') or batch[-1]['observation_id'],
                                     result.get('output') or {'error': result.get('error')},
                                     int(result['seconds'] * 1000))
            result['audit_id'] = audit['id']
            return name, result

        with concurrent.futures.ThreadPoolExecutor(max(1, len(self.assessment))) as pool:
            results = dict(pool.map(one, self.assessment))
        self.cursor = batch[-1]['observation_id']
        medical = (results.get('medical_agent') or {}).get('output')
        for place in (medical or {}).get('places', []):
            self.add_to_medical_queue(place)
        return results

    # ---- medical queue: where people need help, most urgent first (in memory) ----

    def add_to_medical_queue(self, place):
        """Add one place from medical_agent, or merge it into the same place already queued (same building, or
        within 100 m when there is no building). Two reports of "3 trapped" are the same 3 people, so counts
        keep the highest reported, not the sum; priority keeps the most urgent."""
        place = MedicalPlace.model_validate(place).model_dump()
        same = next((q for q in self.medical_queue if _same_place(q, place)), None)
        if same is None:
            self.medical_queue.append(place)
        else:
            for k in ('trapped', 'critical', 'injured'):
                counts = [v for v in (same.get(k), place.get(k)) if v is not None]
                same[k] = max(counts) if counts else None
            if place['priority'] < same['priority']:   # the more urgent report describes the place
                same.update(priority=place['priority'], why=place.get('why') or same.get('why'),
                            name=place.get('name') or same.get('name'))
            same['observation_ids'] += [o for o in place['observation_ids'] if o not in same['observation_ids']]
            same['building_id'] = same.get('building_id') or place.get('building_id')
        self.medical_queue.sort(key=_queue_order)

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
        self.print_medical_queue()
        return len(reports)

    def print_medical_queue(self, top=10):
        if 'medical_agent' not in self.assessment:
            return
        print(f'Medical queue ({len(self.medical_queue)} places, most urgent first):', flush=True)
        for n, q in enumerate(self.medical_queue[:top], 1):
            counts = ', '.join(f'{q[k]} {k}' for k in ('critical', 'trapped', 'injured') if q.get(k)) or 'number unknown'
            where = q.get('name') or q.get('building_id') or q.get('location')
            print(f'  {n}. [P{q["priority"]}] {where}: {counts}. {q.get("why") or ""}', flush=True)

    def watch(self, interval_s=10):
        print(f'Watching for new reports every {interval_s}s (Ctrl+C to stop)...', flush=True)
        while True:
            if not self.cycle():
                time.sleep(interval_s)


def _queue_order(q):
    """Priority first, then most people trapped or critical, then most injured. A place where people were
    reported with no number counts as at least one, so "people trapped" isn't ranked below "1 injured"."""
    counts = [q.get(k) for k in ('critical', 'trapped', 'injured')]
    serious = (q.get('critical') or 0) + (q.get('trapped') or 0) or (1 if all(c is None for c in counts) else 0)
    return q['priority'], -serious, -(q.get('injured') or 0)


def _same_place(a, b, within_m=100):
    if a.get('building_id') and b.get('building_id'):
        return a['building_id'] == b['building_id']
    la, lb = a.get('location') or {}, b.get('location') or {}
    if None in (la.get('longitude'), la.get('latitude'), lb.get('longitude'), lb.get('latitude')):
        return False
    dx = (la['longitude'] - lb['longitude']) * 111_320 * math.cos(math.radians(la['latitude']))
    dy = (la['latitude'] - lb['latitude']) * 110_540
    return math.hypot(dx, dy) <= within_m


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
