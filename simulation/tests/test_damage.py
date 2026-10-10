import collections, json, pathlib, sys
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sim import damage_model as dm
from sim.damage import level_probs, compute_building_damage, shaking
from sim.scenario import Scenario, random_scenario, hypocentral_km
from sim.storage import LocalStorage
from sim.truth import TruthState, DAMAGE_LEVELS


@pytest.fixture(scope='module')
def static_map():
    return LocalStorage().load_static_map(graph=False)


def test_reference_worked_example():
    # docs/REFERENCE.md section 4: Mw 7.3, X = 20 km, ARV 1.8 -> PGV 76 cm/s, I = 5.9, pre-1971 wood ~36 %
    v = dm.pgv_surface(dm.pgv_bedrock600(7.3, 30, 20), arv_jshis=1.8)
    assert v == pytest.approx(75.6, abs=0.1)
    assert dm.jma_intensity(v) == pytest.approx(5.91, abs=0.01)
    assert dm.damage_probs('wood', 'pre1971', v)['collapse'] == pytest.approx(0.358, abs=0.001)
    assert dm.damage_probs('rc', 'post1981', v)['collapse'] == pytest.approx(0.017, abs=0.001)


@pytest.mark.parametrize('pgv', [1, 10, 30, 76, 150, 400])
def test_five_levels_preserve_the_source_curves(pgv):
    for s in ('wood', 'rc', 'steel', 'light_steel'):
        for e in ('pre1971', '1971_1981', 'post1981'):
            p, src = level_probs(s, e, pgv), dm.damage_probs(s, e, pgv)
            assert set(p) == set(DAMAGE_LEVELS)
            assert sum(p.values()) == pytest.approx(1)
            assert all(v >= -1e-12 for v in p.values())
            assert p['collapsed'] == pytest.approx(src['collapse'])
            assert p['severe'] + p['moderate'] == pytest.approx(src['half'])


def test_stronger_shaking_never_reduces_damage():
    for s in ('wood', 'rc'):
        prev = 0
        for pgv in (5, 10, 20, 40, 80, 160):
            p = level_probs(s, 'post1981', pgv)
            assert 1 - p['none'] >= prev
            prev = 1 - p['none']


def test_scenario_is_replayable():
    assert random_scenario(7) == random_scenario(7)
    assert random_scenario(7) != random_scenario(8)
    fixed = random_scenario(7, mw=7.3)   # fixing the magnitude keeps the rest of the scenario
    assert fixed.mw == 7.3 and fixed.depth_km == random_scenario(7).depth_km
    assert all(5.8 <= random_scenario(s).mw <= 8.0 for s in range(200))
    sc = random_scenario(7)
    assert hypocentral_km(sc, 139.7034, 35.6938) >= sc.depth_km


def test_same_seed_same_truth(static_map):
    sc = random_scenario(42)
    a, b = TruthState('a'), TruthState('b')
    compute_building_damage(static_map, sc, a)
    compute_building_damage(static_map, sc, b)
    assert a.building_damage == b.building_damage and a.sampled_attributes == b.sampled_attributes
    assert len(a.building_damage) == len(static_map.buildings) == 5335


def test_sampled_counts_match_expected(static_map):
    sc = Scenario(seed=1, mw=7.3, depth_km=20, epicenter_lon=139.7034, epicenter_lat=35.6938)
    t = TruthState('x')
    compute_building_damage(static_map, sc, t)
    got = collections.Counter(d['damage_level'] for d in t.building_damage.values())
    exp = collections.Counter()
    for bid, props in static_map.buildings.items():
        c = static_map.building_geoms[bid].centroid
        pgv, _ = shaking(sc, c.x, c.y, props['soil_amp'])
        for era, w in props['era_probs'].items():
            for k, v in level_probs(props['structure'], era, pgv).items():
                exp[k] += w * v
    for k in DAMAGE_LEVELS:   # 5 sigma of a binomial is generous; catches a wrong mapping, not noise
        assert abs(got[k] - exp[k]) < 5 * max(exp[k] ** 0.5, 1), (k, got[k], exp[k])


def test_truth_rows_and_dedupe(static_map, tmp_path):
    st = LocalStorage(runs_dir=tmp_path)
    t = TruthState('run1')
    t.set_scenario({'seed': 1})
    compute_building_damage(static_map, random_scenario(3), t)
    rows = list(t.pending_truth_writes)
    assert t.flush(st) == 5336 and t.pending_truth_writes == []
    st.save_truth_batch(rows)   # retry of the same batch must not double count
    lines = [json.loads(l) for l in open(tmp_path / 'run1' / 'truth_building_damage.jsonl')]
    assert len(lines) == 5335
    assert set(lines[0]) == {'event_id', 'scenario_id', 'building_id', 'intensity', 'damage_level',
                             'simulation_time_s', 'sampled_attributes'}
    assert json.loads(lines[0]['sampled_attributes'])['era'] in ('pre1971', '1971_1981', 'post1981')


def test_debris_blocks_only_nearby_roads():
    from shapely.geometry import LineString, box
    from sim.roads import compute_road_damage, KX, KY
    from sim.scenario import CENTER
    from sim.storage import StaticMap
    ll = lambda x, y: (CENTER[0] + x / KX, CENTER[1] + y / KY)   # metres from the station -> lon/lat
    square = lambda x: box(*ll(x, 0), *ll(x + 10, 10))
    street = lambda x: LineString([ll(x, -50), ll(x, 50)])
    sm = StaticMap('t', {'tall': {'height_m': 20}, 'low': {'height_m': 20}, 'ok': {'height_m': 20}},
                   {'tall': square(0), 'low': square(100), 'ok': square(200)}, {},
                   {}, {'near_collapse': street(15), 'near_severe': street(113), 'beyond_reach': street(30),
                        'near_light': street(212)})
    t = TruthState('r')
    for bid, level in (('tall', 'collapsed'), ('low', 'severe'), ('ok', 'light')):
        t.set_building_damage(bid, 6.0, level, 0.0)
    compute_road_damage(sm, t)
    # collapsed 20 m building reaches 10 m, severe reaches 5 m, light sheds nothing
    assert {r: s['status'] for r, s in t.road_status.items()} == {'near_collapse': 'BLOCKED', 'near_severe': 'LIMITED'}
    assert 'tall' in t.road_status['near_collapse']['cause']
