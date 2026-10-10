"""Earthquake -> per-building truth: shaking, intensity and one of the five damage levels.

The physics is damage_model.py (docs/REFERENCE.md). What this module adds is ours and UNCERTAIN:
the split of Murao & Yamazaki's two damage states onto the five levels in truth_building_damage.
"""
import math, random
from . import damage_model as dm
from .scenario import hypocentral_km

SEVERE_SHARE_OF_HALF = 0.4   # 'half' (official partial damage) -> severe, else moderate
LIGHT_LAMBDA_SHIFT = 0.7     # P(light or worse) = the collapse+half curve at about half the PGV (e^-0.7)
DEBRIS_LEVELS = ('severe', 'collapsed')

#each of the 5k buildings in the static map data in the neon database are getting a damage level label

def level_probs(structure, era, pgv_surf):
    """P(each of the five levels). collapsed and severe+moderate match damage_model.damage_probs exactly."""
    p = dm.damage_probs(structure, era, pgv_surf)
    lh, zh = dm.FRAGILITY[structure][era][1]
    p_light_up = max(dm.phi((math.log(max(pgv_surf, 1e-6)) - (lh - LIGHT_LAMBDA_SHIFT)) / zh), 1 - p['none'])
    return {'collapsed': p['collapse'],
            'severe': p['half'] * SEVERE_SHARE_OF_HALF,
            'moderate': p['half'] * (1 - SEVERE_SHARE_OF_HALF),
            'light': p_light_up - (1 - p['none']),
            'none': 1 - p_light_up}


def sample_era(props, rng):
    """Era is always inferred for Tokyo, so it is re-rolled from era_probs every run."""
    x, acc = rng.random(), 0
    for era, v in (props.get('era_probs') or {}).items():
        acc += v
        if x < acc:
            return era
    return props['era']


def shaking(sc, lon, lat, soil_amp):
    """(PGV at the surface in cm/s, JMA intensity) at one point. soil_amp None -> Vs=400 baseline."""
    v600 = dm.pgv_bedrock600(sc.mw, sc.depth_km, hypocentral_km(sc, lon, lat), sc.fault_type)
    v = dm.pgv_surface(v600 * 10 ** sc.pgv_log10_offset, arv_jshis=soil_amp)
    return v, dm.jma_intensity(v)


def compute_building_damage(static_map, sc, truth, sim_time_s=0.0):
    """Fill truth.building_damage for every building. Each building has its own RNG stream
    (seed + building id), so results don't depend on iteration order or on other buildings."""
    for bid, props in static_map.buildings.items():
        rng = random.Random(f'{sc.seed}:damage:{bid}')
        c = static_map.building_geoms[bid].centroid
        pgv, intensity = shaking(sc, c.x, c.y, props.get('soil_amp'))
        era = sample_era(props, rng)
        x, acc, level = rng.random(), 0, 'none'
        for name, p in level_probs(props['structure'], era, pgv).items():
            acc += p
            if x < acc:
                level = name
                break
        truth.set_building_damage(bid, round(intensity, 2), level, sim_time_s, sampled={
            'era': era, 'structure': props['structure'], 'soil_amp': props.get('soil_amp'),
            'soil_amp_source': 'J-SHIS' if props.get('soil_amp') is not None else 'Vs400 baseline',
            'pgv_cm_s': round(pgv, 1)})
