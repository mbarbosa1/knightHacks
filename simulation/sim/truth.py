"""Hidden ground truth for one run, held in memory (docs/simulator-runtime.md). Agents never read this.

Every change also appends a row shaped like the Databricks truth table (database/final-database.md) to
pending_truth_writes; flush() hands them to storage.
"""
import json, uuid

DAMAGE_LEVELS = ('none', 'light', 'moderate', 'severe', 'collapsed')
ROAD_STATUSES = ('OPEN', 'LIMITED', 'BLOCKED')
MATCH_TYPES = ('matched', 'misleading', 'fabricated', 'unmatched')


class TruthState:
    def __init__(self, run_id):
        self.run_id = str(run_id)
        self.building_damage = {}      # building_id -> {intensity, damage_level, sim_time_s}
        self.road_status = {}          # road_id -> {status, cause, sim_time_s}
        self.people = {}               # group_id -> {building_id, count, condition, mobility, language, sim_time_s}
        self.sampled_attributes = {}   # building_id -> {era, structure, ...}
        self.truth_links = []
        self.pending_truth_writes = []

    def _queue(self, table, row, event_id=True):
        row = {'table': table, **({'event_id': str(uuid.uuid4())} if event_id else {}),
               'scenario_id': self.run_id, **row}
        self.pending_truth_writes.append(row)

    def set_scenario(self, row):
        """scenarios has no event_id (one row per run, keyed by id); params is stored as JSON text."""
        row = {**row, 'params': json.dumps(row['params'])} if isinstance(row.get('params'), dict) else row
        self._queue('scenarios', {'id': self.run_id, **row}, event_id=False)
        self.pending_truth_writes[-1].pop('scenario_id')

    def set_building_damage(self, building_id, intensity, damage_level, sim_time_s, sampled=None):
        assert damage_level in DAMAGE_LEVELS, damage_level
        self.building_damage[building_id] = {'intensity': intensity, 'damage_level': damage_level,
                                             'sim_time_s': sim_time_s}
        if sampled is not None:
            self.sampled_attributes[building_id] = sampled
        self._queue('truth_building_damage', {
            'building_id': building_id, 'intensity': intensity, 'damage_level': damage_level,
            'simulation_time_s': sim_time_s,
            'sampled_attributes': json.dumps(self.sampled_attributes.get(building_id))})

    def set_road_status(self, road_id, status, cause, sim_time_s):
        assert status in ROAD_STATUSES, status
        self.road_status[road_id] = {'status': status, 'cause': cause, 'sim_time_s': sim_time_s}
        self._queue('truth_road_status', {'road_id': road_id, 'status': status, 'cause': cause,
                                          'simulation_time_s': sim_time_s})

    def set_people(self, group_id, building_id, count, condition, mobility, language, sim_time_s):
        self.people[group_id] = {'building_id': building_id, 'count': count, 'condition': condition,
                                 'mobility': mobility, 'language': language, 'sim_time_s': sim_time_s}
        self._queue('truth_people', {'id': group_id, 'building_id': building_id, 'count': count,
                                     'condition': condition, 'mobility': mobility, 'language': language,
                                     'simulation_time_s': sim_time_s})

    def add_link(self, observation_id, match_type, entity_type=None, entity_id=None, truth_time_s=None):
        assert match_type in MATCH_TYPES, match_type
        link = {'observation_id': str(observation_id), 'truth_entity_type': entity_type,
                'truth_entity_id': entity_id, 'truth_simulation_time_s': truth_time_s, 'match_type': match_type}
        self.truth_links.append(link)
        self._queue('truth_links', link)   # event_id: every truth table dedupes on it (databricks/tables.sql)

    def flush(self, storage):
        """Hand pending rows to storage; keep them if the write fails so the next flush retries."""
        rows = self.pending_truth_writes
        if rows:
            storage.save_truth_batch(rows)
            self.pending_truth_writes = []
        return len(rows)

    def to_dict(self):
        """Body of GET /runs/{id}/truth (operator reveal-truth only)."""
        return {'run_id': self.run_id, 'building_damage': self.building_damage, 'road_status': self.road_status,
                'people': self.people, 'sampled_attributes': self.sampled_attributes,
                'truth_links': self.truth_links}
