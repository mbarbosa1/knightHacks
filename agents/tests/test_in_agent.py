"""In agent: reading cleanup, contradiction check, instructions file, and the location step. No model calls."""
import pytest
from agents.loader import load_agent

in_agent = load_agent('assesmentAgent/in-agent/in_agent.py')


def obs(**kw):
    base = {'id': '00000000-0000-0000-0000-000000000001', 'run_id': 'run', 'channel': 'emergency_call_119',
            'lang': 'en', 'original_text': 'text', 'claimed_location': None, 'media_url': None,
            'location_precision': 'gps', 'longitude': 139.70, 'latitude': 35.69, 'source_reliability': 0.7,
            'sim_time_s': 10.0}
    return {**base, **kw}


def read(monkeypatch, raw, **obs_kw):
    """read_report with the model replaced by a fixed reply."""
    monkeypatch.setattr(in_agent, 'ask_json', lambda system, user, image_url=None: raw)
    return in_agent.read_report(obs(**obs_kw), 'instructions')


# ---- the instructions file ---------------------------------------------------

def test_instructions_load_without_developer_notes():
    text = in_agent.load_instructions()
    assert text.startswith('# In Agent')
    assert '## Your reply' in text and '## Rules' in text
    assert 'For developers' not in text and not text.endswith('---')


def test_allowed_values_match_the_instructions():
    text = in_agent.load_instructions()
    for value in in_agent.EVENT_TYPES + in_agent.BUILDING_TYPES + in_agent.HAZARDS:
        assert value in text, f'{value} is allowed in code but not mentioned in in-agent.md'


# ---- cleaning the model's reply ----------------------------------------------

def test_clean_reply_is_kept(monkeypatch):
    r = read(monkeypatch, {'event_type': 'building_collapse', 'summary_en': ' Collapse. ', 'building_type': 'office',
                           'building_floors': '9', 'people_trapped': 2, 'count_is_estimate': True,
                           'hazards': ['fire'], 'secondhand': False, 'urgency': 5, 'model_confidence': 0.8})
    assert r['event_type'] == 'building_collapse' and r['summary_en'] == 'Collapse.'
    assert r['building'] == {'type': 'office', 'floors': 9, 'name': None}
    assert r['people']['trapped'] == 2 and r['people']['trapped_mentioned'] and r['people']['count_is_estimate'] is True
    assert r['hazards'] == ['fire'] and r['urgency'] == 5 and r['model_confidence'] == 0.8


def test_made_up_values_become_null_or_other(monkeypatch):
    r = read(monkeypatch, {'event_type': 'meteor', 'building_type': 'castle', 'building_floors': 'tall',
                           'hazards': ['fire', 'zombies'], 'urgency': 99, 'model_confidence': 7})
    assert r['event_type'] == 'other'
    assert r['building']['type'] is None and r['building']['floors'] is None
    assert r['hazards'] == ['fire'] and r['urgency'] == 5 and r['model_confidence'] == 1.0


def test_missing_fields_never_crash(monkeypatch):
    r = read(monkeypatch, {})
    assert r['event_type'] == 'other' and r['urgency'] == 1 and r['hazards'] == []
    assert r['people']['trapped_mentioned'] is False and r['model_confidence'] is None


def test_count_is_estimate_is_null_without_numbers(monkeypatch):
    assert read(monkeypatch, {'event_type': 'building_damage', 'count_is_estimate': False})['people']['count_is_estimate'] is None
    assert read(monkeypatch, {'people_injured': 3, 'count_is_estimate': False})['people']['count_is_estimate'] is False


def test_a_number_means_mentioned(monkeypatch):
    r = read(monkeypatch, {'event_type': 'injuries', 'people_injured': 1, 'injured_mentioned': False})
    assert r['people']['injured_mentioned'] is True and r['event_type'] == 'injuries'


def test_desk_slid_is_not_injuries(monkeypatch):
    """The real mistake found on run 2408c809: 'My desk slid' was read as injuries."""
    r = read(monkeypatch, {'event_type': 'injuries', 'injured_mentioned': False, 'trapped_mentioned': False})
    assert r['event_type'] == 'no_damage'


@pytest.mark.parametrize('event, injured, trapped, building, hazards, expected', [
    ('injuries', False, False, {}, [], 'no_damage'),
    ('injuries', True, False, {}, [], 'injuries'),
    ('injuries', False, False, {'type': 'house'}, [], 'building_damage'),
    ('injuries', False, False, {'floors': 3}, [], 'building_damage'),
    ('people_trapped', False, False, {}, ['fire'], 'other'),
    ('people_trapped', False, True, {}, [], 'people_trapped'),
    ('building_collapse', False, False, {}, [], 'building_collapse'),
    ('made_up', False, False, {}, [], 'other'),
])
def test_consistent_event_type(event, injured, trapped, building, hazards, expected):
    people = {'injured_mentioned': injured, 'trapped_mentioned': trapped}
    assert in_agent._consistent_event_type(event, people, {'type': None, 'floors': None, **building}, hazards) == expected


def test_report_text_and_place_reach_the_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(in_agent, 'ask_json', lambda system, user, image_url=None: seen.update(user=user, image=image_url) or {})
    in_agent.read_report(obs(original_text='ビルが倒壊', lang='ja', claimed_location='Shinjuku 2-chome',
                             media_url='http://x/photo.jpg'), 'instructions')
    assert 'ビルが倒壊' in seen['user'] and 'Shinjuku 2-chome' in seen['user'] and 'Language: ja' in seen['user']
    assert seen['image'] == 'http://x/photo.jpg'


# ---- finding the place ---------------------------------------------------------

class FakeTools:
    def __init__(self):
        self.calls = []

    def find_nearby_buildings(self, lon, lat, radius_m, building_type=None, floors=None):
        self.calls.append(('buildings', radius_m, building_type, floors))
        return {'buildings': [{'building_id': 'bldg_1'}], 'fitting_count': 4}

    def find_nearest_road(self, lon, lat, street_name=None, radius_m=100):
        self.calls.append(('roads', radius_m, street_name))
        return [{'road_id': '1_2_0'}]


def reading(event='building_collapse', street=None, btype=None, floors=None):
    return {'event_type': event, 'street_name': street, 'building': {'type': btype, 'floors': floors, 'name': None}}


@pytest.mark.parametrize('precision, radius', [('gps', 30), ('address', 120), ('landmark', 350)])
def test_search_radius_follows_precision(precision, radius):
    tools = FakeTools()
    loc = in_agent.locate(obs(location_precision=precision), reading(btype='office', floors=9), tools)
    assert tools.calls == [('buildings', radius, 'office', 9)]
    assert loc['uncertainty_m'] == radius and loc['building_candidates'] == [{'building_id': 'bldg_1'}]
    assert loc['buildings_fitting_description'] == 4


def test_vague_report_searches_nothing():
    tools = FakeTools()
    loc = in_agent.locate(obs(location_precision='vague', longitude=None, latitude=None), reading(), tools)
    assert tools.calls == [] and loc['point'] is None and loc['building_candidates'] == []


def test_road_report_searches_roads_on_the_named_street():
    tools = FakeTools()
    loc = in_agent.locate(obs(location_precision='address'), reading('road_blocked', street='Okubo Avenue'), tools)
    assert tools.calls == [('roads', 100, 'Okubo Avenue')] and loc['road_candidates'] == [{'road_id': '1_2_0'}]


def test_no_damage_report_without_building_searches_nothing():
    tools = FakeTools()
    in_agent.locate(obs(), reading('no_damage'), tools)
    assert tools.calls == []


# ---- the agent never stops on one bad report -----------------------------------

def test_normalize_records_errors_instead_of_raising(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError('model down')
    monkeypatch.setattr(in_agent, 'ask_json', broken)
    agent = in_agent.InAgent.__new__(in_agent.InAgent)
    agent.tools, agent.instructions = FakeTools(), 'instructions'
    record = agent.normalize(obs())
    assert record['error'] == 'RuntimeError: model down'
    assert record['observation_id'] == obs()['id'] and record['duration_ms'] >= 0
