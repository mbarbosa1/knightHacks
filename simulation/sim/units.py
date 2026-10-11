"""Responder units: carrying out a dispatch.

This is the permanent half of "who goes where". Something else decides (the agents; a stand-in until
they exist) and calls Fleet.dispatch(unit, destination). The fleet then makes it happen in the world:
the unit drives the real roads at their real speeds, and only finds out a road is blocked when it
gets there. It then reports it first-hand and takes another way.

Planning uses what is *believed* about the roads. Driving meets what is *true*.

On scene the unit does the work its type can do, on the people who are really there: an engine frees
the trapped, an ambulance treats the critical, a patrol looks and reports what it sees. Two units of a
type at one building work twice as fast. When nothing is left for it, the unit reports and is free.

People who are left waiting get worse: from WORSEN_AFTER_S on, every WORSEN_EVERY_S a share of the
trapped at a building become critical and a share of the critical die. A unit with the right skill
working at the building stops that for the people it helps.
"""
import json, math, threading, uuid
from dataclasses import dataclass, field
from .routing import LIMITED_FACTOR, statuses

SKILLS = {'fire_engine': ['rescue'], 'ambulance': ['medical'], 'police': ['assess']}
REROUTE_PAUSE_S = 20      # simulated seconds a unit stands still while a new route is worked out
# Work on scene, in simulated seconds.
RESCUE_S = {'collapsed': 60, 'severe': 40}   # to free one trapped person, by how bad the building is
RESCUE_S_OTHER = 20
TREAT_S = 30              # to treat one critical person
ASSESS_S = 120            # for a patrol to look the scene over and report
NOTHING_S = 60            # a unit that finds nothing it can do stays this long before it is free again
SEARCH_RADIUS_M = 150     # a unit that finds nobody at the address looks this far around it
SEARCH_SPEED_MPS = 2.0    # and gets there on foot at this speed
WORSEN_AFTER_S = 600      # how long people can wait before any of them get worse
WORSEN_EVERY_S = 300      # and how often after that
WORSEN_SHARE = 0.2        # the share that gets worse each time (rounded up: a lone person does)
NEEDS = {'rescue': 'trapped', 'medical': 'critical'}     # skill -> the people it helps
OUTCOME = {'rescue': 'rescued', 'medical': 'treated'}    # skill -> what those people become
RESPONDER_RELIABILITY = 0.98


class DispatchError(Exception):
    """The order cannot be carried out: unknown unit, unit not free, or no way to get there."""


@dataclass
class Unit:
    id: str
    name: str
    type: str
    home_facility_id: str
    node: int
    lon: float
    lat: float
    status: str = 'available'          # available -> en_route -> busy -> available
    skills: list = field(default_factory=list)
    task: dict | None = None


def _along(coords, fraction):
    """The point a fraction of the way along a line of [lon, lat] points."""
    if len(coords) == 1 or fraction <= 0:
        return list(coords[0])
    steps = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(coords, coords[1:])]
    target, walked = sum(steps) * min(fraction, 1.0), 0.0
    for (a, b), step in zip(zip(coords, coords[1:]), steps):
        if walked + step >= target and step > 0:
            f = (target - walked) / step
            return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]
        walked += step
    return list(coords[-1])


class Fleet:
    def __init__(self, run_id, static_map, router, truth, resources, beliefs=None, emit=None, on_blocked=None, on_decision=None):
        """resources: the run's units as saved to ops.resources (id, type, name, home_facility_id, location).
        beliefs(): {road_id: status} that agents currently hold. emit(observation): a first-hand report
        to release. on_blocked(road_ids, observation_id, sim_time_s): record the belief.
        on_decision(row): keep the record of a decision that was made or replaced."""
        self.run_id, self.m, self.router, self.truth = run_id, static_map, router, truth
        self._beliefs, self._emit, self._on_blocked = beliefs or (lambda: {}), emit or (lambda obs: None), on_blocked or (lambda *a: None)
        self.lock = threading.RLock()   # the clock thread moves units while requests dispatch and read them
        self._on_decision = on_decision or (lambda row: None)
        self._versions = {}        # incident_id -> last decision version used
        self.outcomes = {'rescued': 0, 'treated': 0, 'died_waiting': 0}
        self._worsen_next = WORSEN_AFTER_S
        self._groups = {}          # building_id -> {condition: people group id}
        for gid, g in getattr(truth, 'people', {}).items():
            self._groups.setdefault(g['building_id'], {})[g['condition']] = gid
        self.found = {}            # road_id -> sim time a unit found it blocked
        self.log = []              # what happened, newest last: dispatched / blocked / arrived / stuck
        self.units = {}
        for r in resources:
            facility = static_map.facilities[r['home_facility_id']]
            self.units[r['id']] = Unit(r['id'], r['name'], r['type'], r['home_facility_id'], facility['access_node'],
                                       r['location']['longitude'], r['location']['latitude'],
                                       skills=list(SKILLS.get(r['type'], [])))

    # --- what is known about the roads -------------------------------------------------------------
    def believed(self):
        """Agents' road beliefs plus everything units have found for themselves."""
        return {**self._beliefs(), **{r: 'BLOCKED' for r in self.found}}

    def _true_status(self, road_id):
        return self.truth.road_status.get(road_id, {}).get('status', 'OPEN')

    # --- orders ------------------------------------------------------------------------------------
    def available(self, type=None):
        return [u for u in self.units.values() if u.status == 'available' and (type is None or u.type == type)]

    def dispatch(self, unit_id, now_s, lon=None, lat=None, building_id=None, incident_id=None):
        """Send a free unit somewhere. Returns the decision it is now following."""
        with self.lock:
            return self._dispatch(unit_id, now_s, lon, lat, building_id, incident_id)

    def _dispatch(self, unit_id, now_s, lon, lat, building_id, incident_id):
        unit = self.units.get(unit_id)
        if unit is None:
            raise DispatchError(f'No unit {unit_id} in this run')
        if unit.status != 'available':
            raise DispatchError(f'{unit.name} is {unit.status}, not available')
        if building_id is not None:
            if building_id not in self.m.building_geoms:
                raise DispatchError(f'No building {building_id} on this map')
            c = self.m.building_geoms[building_id].centroid
            lon, lat = c.x, c.y
        if lon is None or lat is None:
            raise DispatchError('A destination is needed: building_id, or lon and lat')
        destination = {'lon': lon, 'lat': lat, 'node': self.router.node((lon, lat)),
                       'building_id': building_id, 'incident_id': incident_id}
        route = self.router.route(unit.node, destination['node'], self.believed())
        if route is None:
            raise DispatchError(f'{unit.name} has no way to get there on the roads believed open')
        unit.task = None   # a new job: the last one's trail and routes are not carried over
        self._follow(unit, route, destination, now_s, reason=None)
        unit.status = 'en_route'
        self._record(unit, now_s)
        self.log.append({'t': now_s, 'kind': 'dispatched', 'unit_id': unit.id, 'text': f'{unit.name} dispatched'})
        return self.decision(unit)

    def _next_version(self, incident_id, previous):
        """Versions count up per incident (the table allows one row per incident and version), so a second
        unit sent to the same incident, or a reroute, takes the next number."""
        if incident_id is None:
            return previous.get('version', 0) + 1
        self._versions[incident_id] = self._versions.get(incident_id, 0) + 1
        return self._versions[incident_id]

    def _record(self, unit, at_s, status='sent', invalidated_reason=None):
        """Hand the unit's current decision to whoever keeps the record (ops.decisions)."""
        row = {**self.decision(unit), 'status': status, 'invalidated_reason': invalidated_reason, 'sim_time_s': round(at_s, 1)}
        self._on_decision(row)

    def _follow(self, unit, route, destination, start_s, reason):
        decision_id = str(uuid.uuid4())   # a changed route is a new decision, not an edit of the old one
        version = self._next_version(destination['incident_id'], unit.task or {})
        legs = [(road_id, u, v) for road_id, (u, v) in zip(route['edge_ids'], zip(route['nodes'], route['nodes'][1:]))]
        previous = unit.task or {}
        unit.task = {'decision_id': decision_id, 'version': version, 'reason': reason, 'destination': destination,
                     'legs': legs, 'leg': 0, 'leg_start_s': start_s, 'leg_secs': None, 'leg_coords': None,
                     'route': route['geojson']['coordinates'], 'road_ids': route['edge_ids'],
                     'fallback': (route.get('fallback') or {}).get('geojson', {}).get('coordinates'),
                     'eta_s': start_s + route['travel_time_s'],
                     'trail': previous.get('trail') or [[unit.lon, unit.lat]],
                     'abandoned': previous.get('abandoned', []) + ([previous['route']] if previous.get('route') else [])}

    def decision(self, unit):
        """The plan a unit is following, in the shape of an ops.decisions row."""
        t = unit.task
        if t is None:
            return None
        return {'id': t['decision_id'], 'run_id': self.run_id, 'incident_id': t['destination']['incident_id'],
                'version': t['version'], 'assignments': [{'resource_id': unit.id}],
                'route_geojson': {'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': t['route']},
                                  'properties': {'road_ids': t['road_ids']}},
                'fallback_route_geojson': {'type': 'LineString', 'coordinates': t['fallback']} if t['fallback'] else None,
                'eta_s': round(t['eta_s'], 1), 'status': 'sent', 'invalidated_reason': None}

    # --- time passing ------------------------------------------------------------------------------
    def tick(self, now_s):
        with self.lock:
            for unit in self.units.values():
                if unit.status == 'en_route':
                    self._advance(unit, now_s)
            while True:   # work on scene and people worsening, strictly in time order
                due = [u for u in self.units.values() if u.status == 'busy' and u.task['work']['next_s'] <= now_s]
                first = min(due, key=lambda u: (u.task['work']['next_s'], u.id)) if due else None
                if self._worsen_next <= now_s and (first is None or self._worsen_next <= first.task['work']['next_s']):
                    self._worsen(self._worsen_next)
                    self._worsen_next += WORSEN_EVERY_S
                elif first is not None:
                    self._work(first)
                else:
                    break

    def _advance(self, unit, now_s):
        t, G = unit.task, self.router.G
        while True:
            if t['leg'] >= len(t['legs']):
                return self._arrive(unit, t['leg_start_s'])
            road_id, u, v = t['legs'][t['leg']]
            if t['leg_secs'] is None:
                if now_s < t['leg_start_s']:
                    return                      # still standing where the last route was abandoned
                # Entering a new road: this is the moment the unit meets the truth.
                status = self._true_status(road_id)
                if status == 'BLOCKED':
                    if not self._blocked(unit, road_id, t['leg_start_s']):
                        return
                    t = unit.task
                    continue
                edge = G[u][v][int(road_id.rsplit('_', 1)[1])]
                t['leg_secs'] = edge['travel_time'] * (LIMITED_FACTOR if status == 'LIMITED' else 1)
                t['leg_coords'] = [list(c) for c in edge['geometry'].coords] if edge.get('geometry') is not None else \
                    [[G.nodes[u]['x'], G.nodes[u]['y']], [G.nodes[v]['x'], G.nodes[v]['y']]]
            done_s = t['leg_start_s'] + t['leg_secs']
            if now_s < done_s:
                unit.lon, unit.lat = _along(t['leg_coords'], (now_s - t['leg_start_s']) / t['leg_secs'])
                return
            unit.node, (unit.lon, unit.lat) = v, t['leg_coords'][-1]
            t['trail'] += t['leg_coords'][1:]
            t['leg'], t['leg_start_s'], t['leg_secs'] = t['leg'] + 1, done_s, None

    def _arrive(self, unit, at_s):
        unit.status = 'busy'
        unit.task['arrived_s'] = at_s
        self.log.append({'t': at_s, 'kind': 'arrived', 'unit_id': unit.id, 'text': f'{unit.name} on scene'})
        skill = unit.skills[0] if unit.skills else None
        building_id, walk_s = unit.task['destination']['building_id'], 0.0
        # Reports give addresses, not exact buildings. A crew that finds nobody at the door does what a real
        # one does: it looks around, and goes to the nearest building where it can see people it can help.
        wanted = [skill] if skill in NEEDS else ['rescue', 'medical'] if skill == 'assess' else []
        if wanted and not any(self._waiting(building_id, s) for s in wanted):
            found = self._look_around(unit.task['destination'], wanted)
            if found:
                building_id, metres = found
                unit.task['searched_m'] = round(metres)
                walk_s = metres / SEARCH_SPEED_MPS
        at_s += walk_s
        unit.task['work_at'] = building_id   # where it works; its destination stays the address it was sent to
        secs = ASSESS_S if skill == 'assess' else self._secs_per_person(skill, building_id) if self._waiting(building_id, skill) else None
        unit.task['work'] = {'skill': skill, 'done': 0, 'working': secs is not None and skill != 'assess',
                             'next_s': at_s + (secs or NOTHING_S)}

    # --- work on scene -----------------------------------------------------------------------------
    def _look_around(self, destination, skills):
        """(building_id, metres) of the nearest building within SEARCH_RADIUS_M of the address where people
        are waiting for one of these skills, or None."""
        best = None
        for building_id, groups in self._groups.items():
            if not any(self._waiting(building_id, s) for s in skills):
                continue
            c = self.m.building_geoms[building_id].centroid
            metres = math.hypot((c.x - destination['lon']) * 111_320 * math.cos(math.radians(destination['lat'])),
                                (c.y - destination['lat']) * 110_540)
            if metres <= SEARCH_RADIUS_M and (best is None or metres < best[1]):
                best = (building_id, metres)
        return best

    def _waiting(self, building_id, skill):
        """How many people at this building this skill can still help."""
        gid = self._groups.get(building_id, {}).get(NEEDS.get(skill))
        return self.truth.people[gid]['count'] if gid else 0

    def _secs_per_person(self, skill, building_id):
        if skill == 'medical':
            return TREAT_S
        level = getattr(self.truth, 'building_damage', {}).get(building_id, {}).get('damage_level')
        return RESCUE_S.get(level, RESCUE_S_OTHER)

    def _work(self, unit):
        """The unit has reached the end of one piece of work: one person helped, or its look around done."""
        w, building_id = unit.task['work'], unit.task['work_at']
        at_s = w['next_s']
        if w['working'] and self._waiting(building_id, w['skill']):
            self._help_one(building_id, w['skill'], at_s)
            w['done'] += 1
            if self._waiting(building_id, w['skill']):
                w['next_s'] = at_s + self._secs_per_person(w['skill'], building_id)
                return
        self._leave(unit, at_s)

    def _move_people(self, building_id, n, source, condition, at_s):
        """n people at a building leave one group (by its condition) for another, in the truth."""
        people = self.truth.people
        gid = self._groups[building_id][source]
        g = people[gid]
        self.truth.set_people(gid, building_id, g['count'] - n, g['condition'], g['mobility'], g['language'], at_s)
        to = self._groups[building_id].setdefault(condition, f'{building_id}:{condition}')
        self.truth.set_people(to, building_id, people.get(to, {}).get('count', 0) + n, condition,
                              g['mobility'], g['language'], at_s)

    def _worsen(self, at_s):
        """People still waiting get worse, except where a unit that can help them is at work."""
        helped = {(u.task['work_at'], u.task['work']['skill'])
                  for u in self.units.values() if u.status == 'busy' and u.task['work']['working']}
        for building_id, groups in self._groups.items():
            trapped = 0 if (building_id, 'rescue') in helped else self._waiting(building_id, 'rescue')
            critical = 0 if (building_id, 'medical') in helped else self._waiting(building_id, 'medical')
            if critical:   # counted before anyone joins them: the newly critical are not at risk yet
                dying = math.ceil(critical * WORSEN_SHARE)
                self._move_people(building_id, dying, 'critical', 'deceased', at_s)
                self.outcomes['died_waiting'] += dying
            if trapped:
                self._move_people(building_id, math.ceil(trapped * WORSEN_SHARE), 'trapped', 'critical', at_s)

    def _help_one(self, building_id, skill, at_s):
        """One person changes in the truth: trapped -> rescued, or critical -> treated."""
        self._move_people(building_id, 1, NEEDS[skill], OUTCOME[skill], at_s)
        self.outcomes[OUTCOME[skill]] += 1

    def _leave(self, unit, at_s):
        """Nothing left here for this unit: it reports what it did (or saw) and is free where it stands."""
        w, building_id = unit.task['work'], unit.task['work_at']
        counts = {c: self.truth.people[g]['count'] for c, g in self._groups.get(building_id, {}).items()}
        seen = {c: counts.get(c, 0) for c in ('trapped', 'critical', 'injured', 'deceased')}
        if w['skill'] == 'assess':
            level = getattr(self.truth, 'building_damage', {}).get(building_id, {}).get('damage_level')
            facts = {'action': 'assessed', 'damage_level': level, **seen}
            found = ', '.join(f'{n} {c}' for c, n in seen.items() if n) or 'nobody hurt or trapped'
            text = f"{unit.name} on scene: {'building ' + level if level and level != 'none' else 'no damage to the building'}; {found}."
        elif w['done']:
            left = self._waiting(building_id, w['skill'])
            facts = {'action': OUTCOME[w['skill']], 'count': w['done'], 'still_' + NEEDS[w['skill']]: left}
            did = 'freed' if w['skill'] == 'rescue' else 'treated'
            text = f"{unit.name}: {w['done']} {'person' if w['done'] == 1 else 'people'} {did}. Unit free."
        else:
            facts = {'action': 'nothing_found', **seen}
            text = f"{unit.name}: {'nobody trapped' if w['skill'] == 'rescue' else 'no critical patients'} here. Unit free."
        if unit.task.get('searched_m'):
            facts['searched_m'] = unit.task['searched_m']
            text += f" (found {unit.task['searched_m']} m from the reported address)"
        observation = {
            'id': str(uuid.uuid5(uuid.UUID(self.run_id), f"{unit.id}:{unit.task['decision_id']}:left")), 'run_id': self.run_id,
            'channel': 'responder_update', 'source_reliability': RESPONDER_RELIABILITY, 'format': 'structured', 'lang': 'en',
            'original_text': json.dumps({'unit_id': unit.id, 'incident_id': unit.task['destination']['incident_id'],
                                         'building_id': building_id, **facts}),
            'text_en': text, 'media_url': None, 'claimed_location': None,
            'claimed_geom': {'type': 'Point', 'coordinates': [round(unit.lon, 6), round(unit.lat, 6)]},
            'location_precision': 'gps', 'sim_time_s': round(at_s, 1)}
        if building_id is not None:
            self.truth.add_link(observation['id'], 'matched', 'building', building_id, at_s)
        self._emit(observation)
        unit.status, unit.task['left_s'] = 'available', at_s
        self.log.append({'t': at_s, 'kind': 'left', 'unit_id': unit.id, 'text': text})

    def _blocked(self, unit, road_id, at_s):
        """The unit has reached a road that is really blocked. It reports it and replans.
        Returns False if there is no other way and the unit gives up."""
        u, v, k = road_id.rsplit('_', 2)
        roads = [r for r in (road_id, f'{v}_{u}_{k}') if self._true_status(r) == 'BLOCKED']   # both directions
        for r in roads:
            self.found.setdefault(r, at_s)
        street = self.m.roads.get(road_id, {}).get('name_en')
        observation = {
            'id': str(uuid.uuid5(uuid.UUID(self.run_id), f'{unit.id}:{road_id}')), 'run_id': self.run_id,
            'channel': 'responder_update', 'source_reliability': RESPONDER_RELIABILITY, 'format': 'structured', 'lang': 'en',
            # Structured, so code can act on it without an AI reading it (agreed design).
            'original_text': json.dumps({'unit_id': unit.id, 'road_id': road_id, 'road_ids': roads, 'status': 'BLOCKED'}),
            'text_en': f"{unit.name}: road blocked by debris{' on ' + street if street else ''}. Finding another route.",
            'media_url': None, 'claimed_location': street,
            'claimed_geom': {'type': 'Point', 'coordinates': [round(unit.lon, 6), round(unit.lat, 6)]},
            'location_precision': 'gps', 'sim_time_s': round(at_s, 1)}
        for r in roads:
            self.truth.add_link(observation['id'], 'matched', 'road', r, self.truth.road_status[r]['sim_time_s'])
        self._emit(observation)
        self._on_blocked(roads, observation['id'], at_s)
        t = unit.task
        route = self.router.route(unit.node, t['destination']['node'], self.believed())
        if route is None:
            self._record(unit, at_s, 'invalidated', f'{unit.name}: every way in is blocked')
            unit.status, unit.task['gave_up_s'] = 'available', at_s
            self.log.append({'t': at_s, 'kind': 'stuck', 'unit_id': unit.id, 'road_ids': roads,
                             'text': f'{unit.name} cannot reach the incident: every way in is blocked'})
            return False
        reason = f"{unit.name} found {street or 'a road'} blocked by debris"
        self._record(unit, at_s, 'invalidated', reason)
        self._follow(unit, route, t['destination'], at_s + REROUTE_PAUSE_S, reason)
        self._record(unit, at_s)
        self.log.append({'t': at_s, 'kind': 'blocked', 'unit_id': unit.id, 'road_ids': roads,
                         'text': f'{reason}. Plan v{t["version"]} invalidated, rerouting (v{unit.task["version"]}).'})
        return True

    # --- what others see ---------------------------------------------------------------------------
    def resource_body(self, unit, now_s):
        """A unit as the backend's PUT /runs/{id}/resources/{id} expects it."""
        return {'type': unit.type, 'name': unit.name, 'status': unit.status, 'home_facility_id': unit.home_facility_id,
                'location': {'longitude': round(unit.lon, 6), 'latitude': round(unit.lat, 6)},
                'capacity': None, 'skills': unit.skills, 'sim_time_s': round(now_s, 1)}

    def view(self):
        """Everything the map needs to draw the units: where each is, where it has been, where it is going."""
        with self.lock:
            return self._view()

    def _view(self):
        out = []
        for unit in self.units.values():
            t = unit.task
            out.append({'id': unit.id, 'name': unit.name, 'type': unit.type, 'status': unit.status, 'skills': unit.skills,
                        'lon': round(unit.lon, 6), 'lat': round(unit.lat, 6),
                        'trail': (t['trail'] + [[unit.lon, unit.lat]]) if t else None,
                        'route': t['route'] if t and unit.status == 'en_route' else None,
                        'abandoned_routes': t['abandoned'] if t else [],
                        'destination': {k: t['destination'][k] for k in ('lon', 'lat', 'building_id', 'incident_id')} if t else None,
                        'decision': {'id': t['decision_id'], 'version': t['version'], 'reason': t['reason']} if t else None,
                        'helped': t['work']['done'] if t and t.get('work') else 0})
        return out

    # --- questions agents ask before deciding (always on believed road status) ----------------------
    def place(self, lon=None, lat=None, building_id=None):
        """(lon, lat) of a destination given as a building or a point."""
        if building_id is not None:
            if building_id not in self.m.building_geoms:
                raise DispatchError(f'No building {building_id} on this map')
            c = self.m.building_geoms[building_id].centroid
            return c.x, c.y
        if lon is None or lat is None:
            raise DispatchError('A place is needed: building_id, or lon and lat')
        return lon, lat

    def nearest(self, lon=None, lat=None, building_id=None, type=None, k=3):
        """The free units that can reach a place soonest: the resource agent's shortlist.
        Units with no way there on the roads believed open are left out."""
        with self.lock:
            lon, lat = self.place(lon, lat, building_id)
            candidates = [{'id': u.id, 'type': u.type, 'status': u.status, 'node': u.node} for u in self.units.values()]
            ranked = self.router.nearest_units((lon, lat), candidates, type=type, k=k, status=self.believed())
            return [{'unit_id': r['unit_id'], 'name': self.units[r['unit_id']].name, 'type': r['type'],
                     'skills': self.units[r['unit_id']].skills, 'travel_time_s': r['travel_time_s']} for r in ranked]

    def plan_route(self, unit_id=None, from_lon=None, from_lat=None, lon=None, lat=None, building_id=None):
        """How to get from a unit (or a point) to a place, without sending anyone."""
        with self.lock:
            if unit_id is not None:
                if unit_id not in self.units:
                    raise DispatchError(f'No unit {unit_id} in this run')
                origin = self.units[unit_id].node
            elif from_lon is not None and from_lat is not None:
                origin = (from_lon, from_lat)
            else:
                raise DispatchError('A start is needed: unit_id, or from_lon and from_lat')
            route = self.router.route(origin, self.place(lon, lat, building_id), self.believed())
        if route is None:
            return {'reachable': False}
        fallback = route.get('fallback')
        return {'reachable': True, 'travel_time_s': route['travel_time_s'], 'length_m': route['length_m'],
                'road_ids': route['edge_ids'], 'geometry': route['geojson'],
                'fallback': {'travel_time_s': fallback['travel_time_s'], 'length_m': fallback['length_m'],
                             'road_ids': fallback['edge_ids'], 'geometry': fallback['geojson']} if fallback else None}

    def counts(self):
        with self.lock:
            out = {'available': 0, 'en_route': 0, 'busy': 0}
            for u in self.units.values():
                out[u.status] = out.get(u.status, 0) + 1
            return out
