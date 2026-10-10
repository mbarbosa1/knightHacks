"""Seeded scenario parameters. The same seed always gives the same earthquake.

Units: km, degrees (EPSG:4326). The sampling ranges are ours (UNCERTAIN, tune in calibration);
the physics that consumes them is damage_model.py.
"""
import math, random
from dataclasses import dataclass, asdict

CENTER = (139.7034, 35.6938)   # Shinjuku Station (lon, lat)
PGV_SIGMA_LOG10 = 0.23         # Si & Midorikawa scatter; value marked inferred in docs/REFERENCE.md
MW_RANGE = (5.8, 8.0)          # design decision: smaller events damage nothing here and extrapolate the attenuation formula
DEPTH_KM_RANGE = (10.0, 30.0)
EPICENTER_KM_RANGE = (5.0, 30.0)


@dataclass(frozen=True)
class Scenario:
    seed: int
    mw: float
    depth_km: float
    epicenter_lon: float
    epicenter_lat: float
    fault_type: str = 'crustal'
    pgv_log10_offset: float = 0.0   # one draw per scenario, added to log10 PGV600 everywhere
    hazard_types: tuple = ('earthquake',)

    def params(self):
        """Goes into scenarios.params (everything needed to replay besides the seed)."""
        d = asdict(self)
        del d['seed'], d['hazard_types']
        return d


def haversine_km(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def hypocentral_km(sc, lon, lat):
    """Point source: X = hypocentral distance (docs/REFERENCE.md section 1)."""
    return math.hypot(haversine_km(sc.epicenter_lon, sc.epicenter_lat, lon, lat), sc.depth_km)


def random_scenario(seed, mw=None, scatter=False):
    """mw=None draws the magnitude from MW_RANGE; pass a number to fix it (the UI slider) and keep the
    rest of the scenario seeded. scatter=True adds the N(0, 0.23) log10 PGV term; off by default so a
    given Mw/distance reads the same."""
    rng = random.Random(f'{seed}:scenario')
    drawn = round(rng.uniform(*MW_RANGE), 1)   # always drawn so depth/epicentre don't depend on mw
    mw = drawn if mw is None else float(mw)
    depth = round(rng.uniform(*DEPTH_KM_RANGE), 1)
    dist, bearing = rng.uniform(*EPICENTER_KM_RANGE), rng.uniform(0, 2 * math.pi)
    lon = CENTER[0] + dist * math.sin(bearing) / (111.32 * math.cos(math.radians(CENTER[1])))
    lat = CENTER[1] + dist * math.cos(bearing) / 110.57
    offset = rng.gauss(0, PGV_SIGMA_LOG10)   # always drawn so the stream doesn't depend on the flag
    return Scenario(seed, mw, depth, round(lon, 5), round(lat, 5),
                    pgv_log10_offset=round(offset, 4) if scatter else 0.0)
