import collections, pathlib, random, sys
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sim.damage import compute_building_damage
from sim.people import compute_people, split_by_condition, occupants, CASUALTY_SHARES, CONDITIONS
from sim.scenario import random_scenario
from sim.storage import LocalStorage
from sim.truth import TruthState


@pytest.fixture(scope='module')
def run():
    m = LocalStorage().load_static_map(graph=False)
    sc = random_scenario(42, mw=7.4)
    t = TruthState('p')
    compute_building_damage(m, sc, t)
    compute_people(m, sc, t)
    return m, sc, t


def test_split_adds_up_and_matches_shares():
    rng = random.Random(1)
    got = collections.Counter()
    for _ in range(200):
        c = split_by_condition(500, 'collapsed', rng)
        assert sum(c.values()) == 500 and set(c) <= set(CONDITIONS)
        got.update(c)
    for cond, share in CASUALTY_SHARES['collapsed'].items():
        assert got[cond] / 100000 == pytest.approx(share, abs=0.01)
    assert split_by_condition(40, 'none', rng) == {'fine': 40}
    assert split_by_condition(0, 'collapsed', rng) == {}


def test_occupancy_scales_with_floor_area_and_use():
    rng = random.Random(2)
    office = sum(occupants({'footprint_m2': 500, 'floors': 10, 'usage': 'office'}, rng) for _ in range(300)) / 300
    flats = sum(occupants({'footprint_m2': 500, 'floors': 10, 'usage': 'apartment'}, rng) for _ in range(300)) / 300
    assert office == pytest.approx(200, rel=0.05) and flats == pytest.approx(50, rel=0.05)   # 5000 m2 / 25 and / 100
    assert occupants({'footprint_m2': None, 'floors': None, 'usage': None}, rng) == 0


def test_everyone_is_accounted_for(run):
    m, sc, t = run
    by_building = collections.Counter()
    for gid, g in t.people.items():
        assert gid.startswith(g['building_id']) and g['count'] > 0 and g['condition'] in CONDITIONS
        by_building[g['building_id']] += g['count']
    for bid, n in by_building.items():
        assert n == occupants(m.buildings[bid], random.Random(f'{sc.seed}:people:{bid}'))
    assert 150_000 < sum(by_building.values()) < 350_000   # daytime crowd of central Shinjuku, order of magnitude


def test_harm_follows_damage(run):
    m, sc, t = run
    worst = collections.defaultdict(collections.Counter)
    for g in t.people.values():
        worst[t.building_damage[g['building_id']]['damage_level']][g['condition']] += g['count']
    assert set(worst['none']) == {'fine'}
    assert not {'trapped', 'deceased'} & set(worst['light']) and not {'trapped', 'deceased'} & set(worst['moderate'])
    assert worst['collapsed']['trapped'] > 0
    assert all(g['mobility'] == 'immobile' for g in t.people.values() if g['condition'] in ('trapped', 'critical'))


def test_same_seed_same_people(run):
    m, sc, t = run
    t2 = TruthState('q')
    compute_building_damage(m, sc, t2)
    compute_people(m, sc, t2)
    assert t.people == t2.people
