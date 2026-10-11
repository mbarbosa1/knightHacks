'''
In agent: turns one raw report (119 call, text, social post, radio; any language; maybe a photo) into
one clean, structured record that the detect and medical agents receive in parallel.

    in-agent.md        what the agent does: its instructions (= the model's system prompt), reply format,
                       rules, and the record it hands on (see "For developers" there)
    agents/tools.py    the tools it uses: find_nearby_buildings, find_nearest_road, log_agent_run
    this file          runs it: model reads the words, code finds the place, the record is saved

    from agents.loader import load_agent     # the folder name has a dash, so it is loaded by path
    InAgent = load_agent('assesmentAgent/in-agent/in_agent.py').InAgent
    agent = InAgent.for_run(run_id)          # loads the run's map once
    record = agent.normalize(observation)    # observation = one row of GET /runs/{id}/observations

Run it over a whole simulation run (from the repo root; ./dev.sh running; tunnel to the AMD server open):
    .venv/bin/python agents/assesmentAgent/in-agent/in_agent.py <run_id> [--limit 10] [--workers 6]

Each record is saved in Neon as one ops.agent_runs row (agent 'in_agent', input_ref = observation id) plus
its ops.operational_events row (needs AGENTS_DATABASE_URL in backend/.env). Re-running is safe: reports
with a successful record are skipped, failed ones are retried and their entry replaced.
'''
import argparse, collections, concurrent.futures, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))   # repo root, when run as a script
from agents.llm import ask_json  # noqa: E402
from agents.tools import RunTools  # noqa: E402

AGENT = 'in_agent'
VERSION = '0.2'
INSTRUCTIONS = pathlib.Path(__file__).with_name('in-agent.md')

# Allowed values; must match in-agent.md.
EVENT_TYPES = ('building_collapse', 'building_damage', 'road_blocked', 'road_limited', 'fire',
               'people_trapped', 'injuries', 'no_damage', 'request_info', 'other')
BUILDING_TYPES = ('office', 'commercial', 'apartment', 'house', 'house_with_shop', 'hotel',
                  'education_welfare', 'government', 'other')
HAZARDS = ('fire', 'gas_leak', 'smoke', 'debris', 'flooding', 'power_lines', 'aftershock_risk')
BUILDING_EVENTS = {'building_collapse', 'building_damage', 'people_trapped', 'injuries', 'fire'}
ROAD_EVENTS = {'road_blocked', 'road_limited'}
# How far the real place may be from the report's point (see in-agent.md, "For developers").
SEARCH_RADIUS_M = {'gps': 30, 'address': 120, 'landmark': 350}


def load_instructions():
    """The part of in-agent.md above "## For developers": that is the model's system prompt."""
    text = INSTRUCTIONS.read_text()
    return text.split('## For developers')[0].rstrip().removesuffix('---').rstrip()


# =============================================================================
# 1. READING THE WORDS (the model)
# =============================================================================

def _int_or_none(v):
    try:
        return None if v is None or v == '' else int(v)
    except (TypeError, ValueError):
        return None


def _consistent_event_type(event_type, people, building, hazards):
    """The model's event_type, unless its own fields contradict it: 'injuries' / 'people_trapped' with
    nobody hurt or trapped (e.g. "my desk slid" read as injuries) falls back to building_damage when a
    building is described, 'other' when a hazard is, else no_damage."""
    if event_type not in EVENT_TYPES:
        return 'other'
    if event_type in ('injuries', 'people_trapped') and not (people['injured_mentioned'] or people['trapped_mentioned']):
        if building['type'] or building['floors']:
            return 'building_damage'
        return 'other' if hazards else 'no_damage'
    return event_type


def read_report(obs, instructions):
    """The model's reading of one report, checked and cleaned: values outside the allowed lists become
    null / 'other', so a model slip never reaches the next agents as a made-up category."""
    lines = [f'Channel: {obs["channel"]}', f'Language: {obs["lang"]}', f'Report: {obs["original_text"]}']
    if obs.get('claimed_location'):
        lines.append(f'Place as stated by the sender: {obs["claimed_location"]}')
    raw = ask_json(instructions, '\n'.join(lines), image_url=obs.get('media_url'))

    trapped, injured = _int_or_none(raw.get('people_trapped')), _int_or_none(raw.get('people_injured'))
    evacuated = _int_or_none(raw.get('people_evacuated'))
    estimate, confidence, hazards = raw.get('count_is_estimate'), raw.get('model_confidence'), raw.get('hazards')
    hazards = [h for h in hazards if h in HAZARDS] if isinstance(hazards, list) else []
    building = {'type': raw.get('building_type') if raw.get('building_type') in BUILDING_TYPES else None,
                'floors': _int_or_none(raw.get('building_floors')),
                'name': raw.get('building_name') or None}
    people = {'trapped': trapped, 'injured': injured, 'unhurt_evacuated': evacuated,
              # "about / exact" only means something when the report gives a number
              'count_is_estimate': estimate if isinstance(estimate, bool) and
                                   any(n is not None for n in (trapped, injured, evacuated)) else None,
              'trapped_mentioned': bool(raw.get('trapped_mentioned') or trapped),
              'injured_mentioned': bool(raw.get('injured_mentioned') or injured)}
    return {
        'event_type': _consistent_event_type(raw.get('event_type'), people, building, hazards),
        'summary_en': str(raw.get('summary_en') or '').strip() or None,
        'building': building,
        'people': people,
        'street_name': raw.get('street_name') or None,
        'hazards': hazards,
        'secondhand': bool(raw.get('secondhand')),
        'urgency': min(5, max(1, _int_or_none(raw.get('urgency')) or 1)),
        'model_confidence': round(min(1.0, max(0.0, float(confidence))), 2) if isinstance(confidence, (int, float)) else None,
    }


# =============================================================================
# 2. FINDING THE PLACE (code, with the shared tools)
# =============================================================================

def locate(obs, reading, tools):
    precision = obs.get('location_precision')
    lon, lat = obs.get('longitude'), obs.get('latitude')
    location = {'stated': obs.get('claimed_location'), 'precision': precision,
                'point': [lon, lat] if lon is not None and lat is not None else None,
                'uncertainty_m': None, 'street_name': reading['street_name'],
                'building_candidates': [], 'buildings_fitting_description': None, 'road_candidates': []}
    if location['point'] is None:   # vague ("somewhere around Shinjuku"): no point to search from
        return location
    radius = SEARCH_RADIUS_M.get(precision, 350)
    location['uncertainty_m'] = radius
    building = reading['building']
    if reading['event_type'] in BUILDING_EVENTS or building['type'] or building['floors']:
        found = tools.find_nearby_buildings(lon, lat, radius, building['type'], building['floors'])
        location['building_candidates'] = found['buildings']
        location['buildings_fitting_description'] = found['fitting_count']
    if reading['event_type'] in ROAD_EVENTS or reading['street_name']:
        location['road_candidates'] = tools.find_nearest_road(lon, lat, reading['street_name'],
                                                              radius_m=min(radius, 100))
    return location


# =============================================================================
# 2b. UNIT REPORTS (code only): rescue units send exact facts as JSON (channel responder_update,
#     simulation/sim/units.py). Read them directly: instant, never misread, exact building / road ids.
# =============================================================================

DAMAGE_TO_EVENT = {'collapsed': 'building_collapse', 'severe': 'building_damage', 'moderate': 'building_damage',
                   'light': 'building_damage', 'none': 'no_damage', None: 'other'}


def read_unit_report(obs):
    """(reading, location) for a responder_update, or None when its text is not the units' JSON."""
    try:
        facts = json.loads(obs['original_text'])
    except (TypeError, ValueError):
        return None
    if not isinstance(facts, dict):
        return None
    action, n = facts.get('action'), lambda k: facts.get(k) if isinstance(facts.get(k), int) else None
    trapped = n('still_trapped') if action == 'rescued' else n('trapped')
    critical = n('still_critical') if action == 'treated' else n('critical')
    injured = (critical or 0) + (n('injured') or 0) if critical is not None or n('injured') is not None else None
    if facts.get('status') in ('BLOCKED', 'LIMITED'):
        event = 'road_blocked' if facts['status'] == 'BLOCKED' else 'road_limited'
    elif action == 'assessed':
        event = DAMAGE_TO_EVENT.get(facts.get('damage_level'), 'other')
    elif trapped:
        event = 'people_trapped'
    elif critical:
        event = 'injuries'
    else:
        event = 'no_damage' if action == 'nothing_found' else 'other'
    reading = {
        'event_type': event, 'summary_en': obs.get('text_en') or obs['original_text'],
        'building': {'type': None, 'floors': None, 'name': None},
        'people': {'trapped': trapped, 'injured': injured, 'unhurt_evacuated': None,
                   'count_is_estimate': False if trapped is not None or injured is not None else None,
                   'trapped_mentioned': bool(trapped), 'injured_mentioned': bool(injured)},
        'hazards': [], 'secondhand': False, 'urgency': 5 if (trapped or critical) else 2 if event != 'no_damage' else 1,
        'model_confidence': 1.0,
        'unit_report': facts,           # the raw facts: action, count, unit_id ... for detect / medical
    }
    lon, lat = obs.get('longitude'), obs.get('latitude')
    roads = facts.get('road_ids') or ([facts['road_id']] if facts.get('road_id') else [])
    location = {'stated': None, 'precision': 'exact', 'point': [lon, lat] if lon is not None else None,
                'uncertainty_m': 0, 'street_name': None,
                'building_candidates': [{'building_id': facts['building_id'], 'distance_m': 0.0, 'fits_description': True}]
                                       if facts.get('building_id') else [],
                'buildings_fitting_description': 1 if facts.get('building_id') else None,
                'road_candidates': [{'road_id': r, 'distance_m': 0.0, 'name_match': None} for r in roads]}
    return reading, location


# =============================================================================
# 3. THE AGENT
# =============================================================================

class InAgent:
    def __init__(self, tools):
        self.tools = tools
        self.instructions = load_instructions()

    @classmethod
    def for_run(cls, run_id):
        return cls(RunTools.for_run(run_id))

    def process(self, obs):
        """normalize() and save the record to Neon (ops.agent_runs); returns the record."""
        record = self.normalize(obs)
        self.tools.log_agent_run(AGENT, obs['id'], record, record['duration_ms'], run_key=obs['id'])
        return record

    def normalize(self, obs):
        """One report -> one record. Never raises: a failure is recorded in 'error' so the run continues."""
        started = time.monotonic()
        record = {'observation_id': obs['id'], 'run_id': obs['run_id'], 'channel': obs['channel'],
                  'lang': obs['lang'], 'source_reliability': obs.get('source_reliability'),
                  'sim_time_s': obs.get('sim_time_s'), 'error': None}
        try:
            unit = read_unit_report(obs) if obs.get('channel') == 'responder_update' else None
            if unit:                                  # exact facts from a rescue unit: no model call
                reading, record['location'] = unit
                record.update(reading)
            else:
                reading = read_report(obs, self.instructions)
                record.update({k: v for k, v in reading.items() if k != 'street_name'})
                record['location'] = locate(obs, reading, self.tools)
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
        record['duration_ms'] = int((time.monotonic() - started) * 1000)
        return record


# =============================================================================
# 4. COMMAND LINE: process a whole run
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description='Run the in agent over one simulation run.')
    ap.add_argument('run_id')
    ap.add_argument('--limit', type=int, help='only process this many new reports (for testing)')
    ap.add_argument('--workers', type=int, default=6, help='reports sent to the model at the same time')
    a = ap.parse_args()

    print('Loading the run and its map...', flush=True)
    agent = InAgent.for_run(a.run_id)
    status = agent.tools.ingestion_status()
    if not status.get('ready'):
        raise SystemExit(f'Run {a.run_id} is not ready for agents yet: {status}')
    done = agent.tools.processed_inputs(AGENT)
    todo = [o for o in sorted(agent.tools.list_observations(), key=lambda o: o['sim_time_s']) if o['id'] not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f'{len(done)} reports already done, {len(todo)} to process. Saving to Neon ops.agent_runs.', flush=True)

    events, errors, started = collections.Counter(), 0, time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
        for n, record in enumerate(pool.map(agent.process, todo), 1):
            errors += record['error'] is not None
            events[record.get('event_type', 'error')] += 1
            print(f'[{n}/{len(todo)}] {record.get("event_type", "ERROR"):<18} '
                  f'{(record.get("summary_en") or record["error"] or "")[:90]}', flush=True)
    print(f'\nDone in {time.monotonic() - started:.0f}s: {dict(events)}; {errors} errors')


if __name__ == '__main__':
    main()
