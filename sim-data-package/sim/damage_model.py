"""Reference implementation of the shaking -> intensity -> damage chain in REFERENCE.md.

Pure Python, no dependencies. Units: km for distances/depth, cm/s for PGV.
Run `python3 damage_model.py` for a worked example.
"""
import math, random

# --- 1. Ground motion: Si & Midorikawa (1999), PGV on Vs=600 m/s bedrock -------------------
FAULT_TYPE_D = {'crustal': 0.0, 'interplate': -0.02, 'intraplate': 0.12}


def pgv_bedrock600(mw, depth_km, dist_km, fault_type='crustal'):
    """Si & Midorikawa (1999). dist_km = shortest distance to the fault rupture plane."""
    d = FAULT_TYPE_D[fault_type]
    log_pgv = (0.58 * mw + 0.0038 * depth_km + d - 1.29
               - math.log10(dist_km + 0.0028 * 10 ** (0.50 * mw)) - 0.002 * dist_km)
    return 10 ** log_pgv


# --- 2. Site amplification -------------------------------------------------------------------
def pgv_surface(pgv600, arv_jshis=None, avs30=None):
    """J-SHIS ARV is relative to Vs=400 m/s, so convert 600 -> 400 with x1.31 first.
    Without J-SHIS, derive ARV (relative to Vs=600) from AVS30 (Matsuoka & Midorikawa 1994)."""
    if arv_jshis is not None:
        return pgv600 * 1.31 * arv_jshis
    if avs30 is not None:
        return pgv600 * 10 ** (1.83 - 0.66 * math.log10(avs30))
    return pgv600 * 1.31  # Vs=400 baseline


# --- 3. JMA intensity: Midorikawa et al. (1999), valid 4 <= I <= 7 ---------------------------
def jma_intensity(pgv_surf):
    return 2.68 + 1.72 * math.log10(max(pgv_surf, 1e-6))


def shindo_class(i):
    for lim, name in ((0.5, '0'), (1.5, '1'), (2.5, '2'), (3.5, '3'), (4.5, '4'), (5.0, '5-'),
                      (5.5, '5+'), (6.0, '6-'), (6.5, '6+')):
        if i < lim: return name
    return '7'


# --- 4. Fragility: Murao & Yamazaki (2000), P = Phi((ln PGV - lambda) / zeta) ----------------
# (lambda, zeta) for collapse (zenkai) and collapse+half-collapse (zenhankai). PGV in cm/s.
FRAGILITY = {
    'wood':        {'pre1971': ((4.45, 0.342), (4.02, 0.456)),   # 1962-71 bin
                    '1971_1981': ((4.73, 0.378), (4.25, 0.395)),
                    'post1981': ((5.12, 0.496), (4.61, 0.474))},  # 1982-94 bin
    'rc':          {'pre1971': ((5.12, 0.646), (4.72, 0.691)),
                    '1971_1981': ((5.33, 0.575), (4.85, 0.612)),
                    'post1981': ((6.00, 0.789), (5.33, 0.789))},
    'steel':       {'pre1971': ((4.64, 0.619), (4.25, 0.712)),
                    '1971_1981': ((4.97, 0.490), (4.49, 0.549)),
                    'post1981': ((5.64, 0.731), (5.01, 0.733))},
    'light_steel': {'pre1971': ((4.70, 0.550), (4.41, 0.504)),
                    '1971_1981': ((5.82, 0.972), (4.95, 0.855)),
                    'post1981': ((6.19, 1.101), (5.28, 0.865))},
}
FRAGILITY['src'] = FRAGILITY['rc']


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def damage_probs(structure, era, pgv_surf):
    (lc, zc), (lh, zh) = FRAGILITY[structure][era]
    ln = math.log(max(pgv_surf, 1e-6))
    p_collapse = phi((ln - lc) / zc)
    p_any = max(phi((ln - lh) / zh), p_collapse)
    return {'collapse': p_collapse, 'half': p_any - p_collapse, 'none': 1 - p_any}


def sample_damage(props, pgv_surf, rng=random):
    """props = one feature's properties from shinjuku_buildings.geojson.
    Resamples era from era_probs when the era is inferred, so Monte Carlo runs carry that uncertainty."""
    era = props['era']
    if props.get('era_probs'):
        x, acc = rng.random(), 0
        for e, v in props['era_probs'].items():
            acc += v
            if x < acc: era = e; break
    p = damage_probs(props['structure'], era, pgv_surf)
    x = rng.random()
    return 'collapse' if x < p['collapse'] else 'half' if x < p['collapse'] + p['half'] else 'none'


if __name__ == '__main__':
    # Tokyo-south-inland-type event (Mw 7.3), site 20 km from the fault plane, soft ground (J-SHIS ARV 1.8)
    v600 = pgv_bedrock600(7.3, 30, 20)
    v = pgv_surface(v600, arv_jshis=1.8)
    i = jma_intensity(v)
    print(f'PGV600={v600:.1f} cm/s  surface={v:.1f} cm/s  I={i:.2f} (shindo {shindo_class(i)})')
    for s in ('wood', 'rc', 'steel'):
        for e in ('pre1971', '1971_1981', 'post1981'):
            p = damage_probs(s, e, v)
            print(f'  {s:6s} {e:10s} collapse={p["collapse"]:.3f}  half={p["half"]:.3f}')
