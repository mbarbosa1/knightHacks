"""Turn a stated place into map ids. Agents cannot know that a stretch of Meiji-dori is road
'158030410_472383038_0'; this is the code step that finds it from a point and, if given, a street name."""
import re
from shapely import STRtree
from shapely.geometry import Point
from .roads import to_metres

_NOISE = re.compile(r'\b(avenue|ave|street|st|dori|road|rd)\b|[\s.\-]')


def _norm(name):
    return _NOISE.sub('', (name or '').lower().replace('ō', 'o').replace('ū', 'u'))


class MapIndex:
    def __init__(self, static_map):
        self.m = static_map
        self._road_ids = list(static_map.road_geoms)
        self._road_m = [to_metres(static_map.road_geoms[i]) for i in self._road_ids]
        self._roads = STRtree(self._road_m)
        self._bldg_ids = list(static_map.building_geoms)
        self._bldg_m = [to_metres(static_map.building_geoms[i]) for i in self._bldg_ids]
        self._bldgs = STRtree(self._bldg_m)

    def roads_near(self, lon, lat, street_name=None, radius_m=60, k=6):
        """Road segments within radius_m of a point, nearest first. With a street name, segments whose English
        or Japanese name matches are searched out to 4x the radius and listed before the rest.
        Both directions of a two-way street are separate ids and both are returned."""
        p = to_metres(Point(lon, lat))
        want = _norm(street_name)
        out = []
        for i in self._roads.query(p.buffer(radius_m * (4 if want else 1))):
            props = self.m.roads[self._road_ids[i]]
            d = self._road_m[i].distance(p)
            named = bool(want) and any(want in _norm(n) or (_norm(n) and _norm(n) in want)
                                       for n in (props.get('name_en'), props.get('name_ja')))
            if d <= radius_m or named:
                out.append({'road_id': self._road_ids[i], 'name_en': props.get('name_en'), 'name_ja': props.get('name_ja'),
                            'distance_m': round(d, 1), 'name_match': named})
        return sorted(out, key=lambda r: (not r['name_match'], r['distance_m']))[:k]

    def building_at(self, lon, lat, radius_m=30):
        """The building containing the point, else the nearest within radius_m, else None."""
        p = to_metres(Point(lon, lat))
        best = None
        for i in self._bldgs.query(p.buffer(radius_m)):
            d = self._bldg_m[i].distance(p)
            if d <= radius_m and (best is None or d < best[1]):
                best = (self._bldg_ids[i], d)
        if best is None:
            return None
        props = self.m.buildings[best[0]]
        return {'building_id': best[0], 'distance_m': round(best[1], 1), 'usage': props.get('usage'),
                'floors': props.get('floors'), 'structure': props.get('structure'), 'name_en': props.get('name_en')}
