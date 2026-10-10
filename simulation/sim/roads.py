"""Debris from damaged buildings -> truth road status. First cut: the reach numbers are heuristics (UNCERTAIN).

A collapsed building spills debris up to half its height from its footprint and blocks what it touches;
a severely damaged one sheds a quarter of its height and limits the road. Roads untouched stay OPEN and
are not written (absent = OPEN).
"""
import math
from shapely import STRtree, transform
from .scenario import CENTER

DEBRIS = {'collapsed': (0.5, 'BLOCKED'), 'severe': (0.25, 'LIMITED')}   # level -> (share of height, status)
MIN_REACH_M, MAX_REACH_M = 2.0, 20.0   # tall buildings don't topple full length
RANK = {'OPEN': 0, 'LIMITED': 1, 'BLOCKED': 2}
KX, KY = 111320 * math.cos(math.radians(CENTER[1])), 110570

#which roads in the map are damaged

def to_metres(geom):
    """Local flat projection around Shinjuku Station; fine over a 2 km box."""
    return transform(geom, lambda xy: (xy - CENTER) * (KX, KY))


def compute_road_damage(static_map, truth, sim_time_s=0.0):
    ids = list(static_map.road_geoms)
    tree = STRtree([to_metres(static_map.road_geoms[i]) for i in ids])
    worst = {}
    for bid, d in truth.building_damage.items():
        if d['damage_level'] not in DEBRIS:
            continue
        share, status = DEBRIS[d['damage_level']]
        reach = min(max((static_map.buildings[bid].get('height_m') or 0) * share, MIN_REACH_M), MAX_REACH_M)
        for i in tree.query(to_metres(static_map.building_geoms[bid]).buffer(reach), predicate='intersects'):
            rid = ids[i]
            if RANK[status] > RANK[worst.get(rid, ('OPEN',))[0]]:
                worst[rid] = (status, f'debris from {d["damage_level"]} building {bid}')
    for rid, (status, cause) in worst.items():
        truth.set_road_status(rid, status, cause, sim_time_s)
