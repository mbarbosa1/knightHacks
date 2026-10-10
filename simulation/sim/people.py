"""Who is in each building when the earthquake hits, and what happens to them -> truth.people.

Every number in this module is our own assumption (UNCERTAIN, tunable): there is no occupancy data in
PLATEAU, and the casualty shares only follow the general shape of published casualty models (most
serious harm happens in collapsed buildings). Nothing here is calibrated.
"""
import random

# Gross floor area (m2) per person actually present on a weekday at noon, by building use.
# The demo always runs at this time; other times of day would need their own table.
M2_PER_PERSON_WEEKDAY_NOON = {
    'office': 25, 'commercial': 25, 'mixed_commercial': 30, 'education_welfare': 20, 'government': 30,
    'hotel': 80, 'apartment': 100, 'house': 110, 'house_with_shop': 60, 'house_with_workshop': 60,
    'transport_warehouse': 100, 'factory': 50, 'utility': 200,
}
DEFAULT_M2_PER_PERSON = 60
COUNT_JITTER = 0.2   # +-20 % per building per run

# Share of a building's occupants in each condition, by damage level. 'fine' is whatever is left.
CASUALTY_SHARES = {
    'collapsed': {'deceased': 0.10, 'critical': 0.05, 'trapped': 0.20, 'injured': 0.30},
    'severe':    {'deceased': 0.001, 'critical': 0.005, 'trapped': 0.01, 'injured': 0.08},
    'moderate':  {'critical': 0.0005, 'injured': 0.02},
    'light':     {'injured': 0.003},
    'none':      {},
}
CONDITIONS = ('fine', 'injured', 'trapped', 'critical', 'deceased')
LANGUAGES = (('ja', 0.82), ('en', 0.08), ('zh', 0.06), ('ko', 0.04))   # main language of a group
MOBILITY = {'fine': 'ambulatory', 'trapped': 'immobile', 'critical': 'immobile', 'deceased': None}
INJURED_NEEDS_HELP = 0.3   # share of injured groups that cannot walk out unaided


def occupants(props, rng):
    area = (props.get('footprint_m2') or 0) * (props.get('floors') or 1)
    expected = area / M2_PER_PERSON_WEEKDAY_NOON.get(props.get('usage'), DEFAULT_M2_PER_PERSON)
    expected *= 1 + rng.uniform(-COUNT_JITTER, COUNT_JITTER)
    return int(expected) + (rng.random() < expected % 1)   # randomised rounding keeps small houses from all being 0


def split_by_condition(n, damage_level, rng):
    """{condition: count} summing to n. Each person is drawn independently."""
    out, left = {}, n
    remaining_share = 1.0
    for cond, share in CASUALTY_SHARES[damage_level].items():
        k = rng.binomialvariate(left, min(share / remaining_share, 1.0)) if left else 0
        if k:
            out[cond] = k
        left -= k
        remaining_share -= share
    if left:
        out['fine'] = left
    return out


def compute_people(static_map, sc, truth, sim_time_s=0.0):
    """One group per building per condition. Group ids are stable for the run; a later change of
    condition is written as a new row with the same id (truth_people is one row per change)."""
    for bid, props in static_map.buildings.items():
        rng = random.Random(f'{sc.seed}:people:{bid}')
        n = occupants(props, rng)
        if not n:
            continue
        counts = split_by_condition(n, truth.building_damage[bid]['damage_level'], rng)
        for k, cond in enumerate(c for c in CONDITIONS if c in counts):
            language = rng.choices([l for l, _ in LANGUAGES], [w for _, w in LANGUAGES])[0]
            mobility = ('needs_assistance' if rng.random() < INJURED_NEEDS_HELP else 'ambulatory') \
                if cond == 'injured' else MOBILITY[cond]
            truth.set_people(f'{bid}:g{k}', bid, counts[cond], cond, mobility, language, sim_time_s)
