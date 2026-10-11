-- ============================================================================
-- Reveal truth: score what the agents believed against what really happened, for one run.
-- Run by:  cd simulation && ../.venv/bin/python -m sim.databricks_sink --score <run_id>
--          (copies the run's Neon event log first, then runs this; parameters :run_id, :agent_config)
--
-- Agent beliefs come from history.simulation_events (Neon ops.operational_events copied after/while the
-- run plays): the latest state of every incident and road restriction the agents wrote.
-- The truth comes from truth.* (uploaded by the simulator). Only reports that were actually released to
-- Neon count: a run stopped early is scored on what the agents could have seen.
--
-- How an incident is tied to the truth: its building_id, or any report it cites as evidence
-- (evidence.supports) whose answer key (truth_links, match_type 'matched') points at a building or at
-- a group of people in a building. Incidents with status 'disputed' count as rejected by the agents.
--
-- One row per metric (long format), so a new metric needs no schema change:
--   rescue_found_rate      buildings with trapped/critical people that were reported, and have an incident
--   rescue_people_found    same, counted in people
--   incident_precision     share of the agents' incidents that are about real damage or people in need
--   rumors_acted_on        incidents built only on fabricated/misleading reports (lower is better)
--   rumors_caught          share of fabricated/misleading reports the agents did not turn into an incident
--   duplicate_incidents    extra incidents for a building that already had one (lower is better)
--   road_block_recall      reported blocked/limited roads the agents marked blocked/limited
--   road_block_precision   roads the agents marked blocked/limited that really are
--   people_helped_by_units rescued + treated by the simulated units by the end of the run
--   died_waiting           people who died during the run while waiting for help (lower is better)
-- Rescue need is measured at the start of the run (people_start): units rescue people and the waiting get
-- worse while a run plays, so the latest state would understate who needed help.
-- ============================================================================
INSERT INTO knighthacks.eval.scorecards (scenario_id, metric, value, details, agent_config, scored_at)
WITH
events AS (
  SELECT * FROM knighthacks.history.simulation_events WHERE run_id = :run_id
),
released AS (                    -- reports the agents could see
  SELECT DISTINCT row_id AS observation_id FROM events WHERE table_name = 'observations'
),
people AS (                      -- every group of people, latest state (for mapping reports to buildings)
  SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY id ORDER BY simulation_time_s DESC) AS rn
                 FROM knighthacks.truth.truth_people WHERE scenario_id = :run_id) WHERE rn = 1
),
people_start AS (                -- who needed help when the earthquake hit: units rescue people and the waiting
  SELECT * FROM knighthacks.truth.truth_people   -- get worse during a run, so the need is measured at the start
  WHERE scenario_id = :run_id
    AND simulation_time_s = (SELECT min(simulation_time_s) FROM knighthacks.truth.truth_people WHERE scenario_id = :run_id)
),
damage AS (
  SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY building_id ORDER BY simulation_time_s DESC) AS rn
                 FROM knighthacks.truth.truth_building_damage WHERE scenario_id = :run_id) WHERE rn = 1
),
roads AS (
  SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY road_id ORDER BY simulation_time_s DESC) AS rn
                 FROM knighthacks.truth.truth_road_status WHERE scenario_id = :run_id) WHERE rn = 1
),
links AS (                       -- answer key of the released reports
  SELECT l.* FROM knighthacks.truth.truth_links l JOIN released r ON l.observation_id = r.observation_id
  WHERE l.scenario_id = :run_id
),
report_building AS (             -- report -> the building it is really about (matched reports only)
  SELECT DISTINCT l.observation_id,
         coalesce(CASE WHEN l.truth_entity_type = 'building' THEN l.truth_entity_id END, p.building_id) AS building_id
  FROM links l LEFT JOIN people p ON l.truth_entity_type = 'people' AND p.id = l.truth_entity_id
  WHERE l.match_type = 'matched'
),
report_kind AS (                 -- a rumour: every link of the report is fabricated or misleading
  SELECT observation_id, min(CASE WHEN match_type IN ('fabricated', 'misleading') THEN 1 ELSE 0 END) = 1 AS is_rumor
  FROM links GROUP BY observation_id
),
rescue AS (                      -- buildings with people who need rescue, and whether any report mentions them
  SELECT p.building_id, sum(p.count) AS people,
         p.building_id IN (SELECT building_id FROM report_building WHERE building_id IS NOT NULL) AS reported
  FROM people_start p
  WHERE p.condition IN ('trapped', 'critical')
  GROUP BY p.building_id
),
incidents AS (                   -- latest state of each incident the agents wrote
  SELECT row_id AS incident_id,
         get_json_object(payload, '$.row.status') AS status,
         get_json_object(payload, '$.row.building_id') AS building_id,
         coalesce(from_json(get_json_object(payload, '$.row.evidence.supports'), 'ARRAY<STRING>'), array()) AS supports
  FROM (SELECT *, row_number() OVER (PARTITION BY row_id ORDER BY created_at DESC, event_id DESC) AS rn
        FROM events WHERE table_name = 'incidents') WHERE rn = 1
),
active AS (SELECT * FROM incidents WHERE coalesce(status, '') <> 'disputed'),
incident_support AS (            -- incident -> each report it cites (null row when it cites none)
  SELECT incident_id, explode_outer(supports) AS observation_id FROM active
),
incident_building AS (           -- incident -> real buildings it is about
  SELECT incident_id, building_id FROM active WHERE building_id IS NOT NULL
  UNION
  SELECT s.incident_id, rb.building_id
  FROM incident_support s JOIN report_building rb ON rb.observation_id = s.observation_id
  WHERE rb.building_id IS NOT NULL
),
real_incidents AS (              -- about a damaged building or one with people in need
  SELECT DISTINCT ib.incident_id
  FROM incident_building ib
  LEFT JOIN damage d ON d.building_id = ib.building_id
  LEFT JOIN rescue r ON r.building_id = ib.building_id
  WHERE d.damage_level IN ('moderate', 'severe', 'collapsed') OR r.building_id IS NOT NULL
),
incident_quality AS (
  SELECT s.incident_id,
         max(CASE WHEN ri.incident_id IS NOT NULL THEN 1 ELSE 0 END) = 1 AS is_real,
         count(s.observation_id) > 0
           AND count_if(s.observation_id IS NOT NULL AND NOT coalesce(k.is_rumor, false)) = 0 AS only_rumors
  FROM incident_support s
  LEFT JOIN report_kind k ON k.observation_id = s.observation_id
  LEFT JOIN real_incidents ri ON ri.incident_id = s.incident_id
  GROUP BY s.incident_id
),
rumors AS (
  SELECT k.observation_id, b.observation_id IS NOT NULL AS believed
  FROM report_kind k
  LEFT JOIN (SELECT DISTINCT observation_id FROM incident_support) b ON b.observation_id = k.observation_id
  WHERE k.is_rumor
),
road_beliefs AS (                -- latest status of each road the agents marked
  SELECT row_id AS road_id, get_json_object(payload, '$.row.status') AS status
  FROM (SELECT *, row_number() OVER (PARTITION BY row_id ORDER BY created_at DESC, event_id DESC) AS rn
        FROM events WHERE table_name = 'road_restrictions') WHERE rn = 1
),
reported_bad_roads AS (          -- really blocked/limited roads that some released report is about
  SELECT DISTINCT r.road_id FROM roads r
  JOIN links l ON l.truth_entity_type = 'road' AND l.truth_entity_id = r.road_id AND l.match_type = 'matched'
  WHERE r.status IN ('BLOCKED', 'LIMITED')
),
covered AS (SELECT DISTINCT building_id FROM incident_building),
metrics AS (
  SELECT 'rescue_found_rate' AS metric,
         try_divide(count_if(r.reported AND c.building_id IS NOT NULL), count_if(r.reported)) AS value,
         to_json(named_struct('found', count_if(r.reported AND c.building_id IS NOT NULL),
                              'reported', count_if(r.reported), 'in_truth', count(*))) AS details
  FROM rescue r LEFT JOIN covered c ON c.building_id = r.building_id
  UNION ALL
  SELECT 'rescue_people_found',
         try_divide(sum(CASE WHEN r.reported AND c.building_id IS NOT NULL THEN r.people ELSE 0 END),
                    sum(CASE WHEN r.reported THEN r.people ELSE 0 END)),
         to_json(named_struct('people_found', sum(CASE WHEN r.reported AND c.building_id IS NOT NULL THEN r.people ELSE 0 END),
                              'people_reported', sum(CASE WHEN r.reported THEN r.people ELSE 0 END),
                              'people_in_truth', sum(r.people)))
  FROM rescue r LEFT JOIN covered c ON c.building_id = r.building_id
  UNION ALL
  SELECT 'incident_precision', try_divide(count_if(is_real), count(*)),
         to_json(named_struct('real', count_if(is_real), 'active_incidents', count(*)))
  FROM incident_quality
  UNION ALL
  SELECT 'rumors_acted_on', CAST(count_if(only_rumors) AS DOUBLE),
         to_json(named_struct('incidents_only_from_rumors', count_if(only_rumors), 'active_incidents', count(*)))
  FROM incident_quality
  UNION ALL
  SELECT 'rumors_caught', try_divide(count_if(NOT believed), count(*)),
         to_json(named_struct('ignored', count_if(NOT believed), 'rumor_reports_released', count(*)))
  FROM rumors
  UNION ALL
  SELECT 'duplicate_incidents', CAST(coalesce(sum(n - 1), 0) AS DOUBLE),
         to_json(named_struct('buildings_with_duplicates', count(*)))
  FROM (SELECT building_id, count(DISTINCT incident_id) AS n FROM incident_building GROUP BY building_id HAVING n > 1)
  UNION ALL
  SELECT 'people_helped_by_units',              -- rescued + treated by the simulated units, by the end of the run
         CAST(coalesce(sum(CASE WHEN condition IN ('rescued', 'treated') THEN count END), 0) AS DOUBLE),
         to_json(named_struct('rescued', coalesce(sum(CASE WHEN condition = 'rescued' THEN count END), 0),
                              'treated', coalesce(sum(CASE WHEN condition = 'treated' THEN count END), 0)))
  FROM people
  UNION ALL
  SELECT 'died_waiting',                         -- deceased now minus deceased when the earthquake hit (lower is better)
         CAST(greatest(coalesce((SELECT sum(count) FROM people WHERE condition = 'deceased'), 0)
                     - coalesce((SELECT sum(count) FROM people_start WHERE condition = 'deceased'), 0), 0) AS DOUBLE),
         to_json(named_struct('deceased_at_start', coalesce((SELECT sum(count) FROM people_start WHERE condition = 'deceased'), 0),
                              'deceased_now', coalesce((SELECT sum(count) FROM people WHERE condition = 'deceased'), 0)))
  UNION ALL
  SELECT 'road_block_recall', try_divide(count_if(b.road_id IS NOT NULL), count(*)),
         to_json(named_struct('marked', count_if(b.road_id IS NOT NULL), 'reported_bad_roads', count(*)))
  FROM reported_bad_roads t
  LEFT JOIN road_beliefs b ON b.road_id = t.road_id AND b.status IN ('BLOCKED', 'LIMITED')
  UNION ALL
  SELECT 'road_block_precision', try_divide(count_if(r.status IN ('BLOCKED', 'LIMITED')), count(*)),
         to_json(named_struct('correct', count_if(r.status IN ('BLOCKED', 'LIMITED')), 'marked', count(*)))
  FROM road_beliefs b LEFT JOIN roads r ON r.road_id = b.road_id
  WHERE b.status IN ('BLOCKED', 'LIMITED')
)
SELECT :run_id, metric, CAST(value AS DOUBLE), details, :agent_config, current_timestamp() FROM metrics
