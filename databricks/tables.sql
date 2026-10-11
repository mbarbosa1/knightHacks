-- Databricks (Unity Catalog) tables, matching database/final-database.md (v1), "System 2".
-- Already created in the team workspace. This file records them so they can be rebuilt.
-- Run in a Databricks SQL editor / warehouse. Safe to re-run: everything is IF NOT EXISTS.
--
-- Agents get NO Databricks credentials. Only the simulator (truth) and the export/scoring
-- jobs (history, eval) connect here.

CREATE CATALOG IF NOT EXISTS knighthacks
    COMMENT 'Disaster-response simulator: hidden ground truth, run history, evaluation';

CREATE SCHEMA IF NOT EXISTS knighthacks.truth
    COMMENT 'Hidden ground truth written by the simulator. Agents have no access.';
CREATE SCHEMA IF NOT EXISTS knighthacks.history
    COMMENT 'Completed-run operational history exported from Neon ops.operational_events';
CREATE SCHEMA IF NOT EXISTS knighthacks.eval
    COMMENT 'Scorecards comparing agent beliefs (Neon) with ground truth';

-- ============================================================================
-- truth: what really happened (written by the simulator via save_truth_batch)
-- Every truth row has event_id; writers MERGE on it so retries never double count.
-- ============================================================================

CREATE TABLE IF NOT EXISTS knighthacks.truth.scenarios (
    id                STRING NOT NULL COMMENT 'Run id, equals Neon ops.simulation_runs.id',
    seed              BIGINT COMMENT 'Random seed; same seed reproduces the same earthquake and damage',
    hazard_types      ARRAY<STRING> COMMENT 'e.g. [earthquake]',
    params            STRING COMMENT 'JSON: magnitude, depth_km, epicentre, etc.',
    map_version_id    STRING COMMENT 'Static map version, e.g. shinjuku-v1',
    simulator_version STRING,
    started_at        TIMESTAMP,
    ended_at          TIMESTAMP
) COMMENT 'One row per simulation run (scenario parameters)';

CREATE TABLE IF NOT EXISTS knighthacks.truth.truth_building_damage (
    event_id           STRING NOT NULL COMMENT 'Unique per truth change; dedupe key',
    scenario_id        STRING NOT NULL COMMENT 'scenarios.id',
    building_id        STRING NOT NULL COMMENT 'Neon static.buildings.id',
    intensity          DOUBLE COMMENT 'JMA seismic intensity at the building',
    damage_level       STRING NOT NULL COMMENT 'none / light / moderate / severe / collapsed',
    simulation_time_s  DOUBLE COMMENT 'Seconds since the run started',
    sampled_attributes STRING COMMENT 'JSON: per-run sampled era, structure, etc.'
) COMMENT 'True damage state of each building; new row per change';

CREATE TABLE IF NOT EXISTS knighthacks.truth.truth_road_status (
    event_id          STRING NOT NULL COMMENT 'Unique per truth change; dedupe key',
    scenario_id       STRING NOT NULL,
    road_id           STRING NOT NULL COMMENT 'Road segment id <u>_<v>_<key>, same as Neon and the routing graph',
    status            STRING COMMENT 'True road status (expected OPEN / LIMITED / BLOCKED)',
    cause             STRING COMMENT 'e.g. debris from collapsed building',
    simulation_time_s DOUBLE
) COMMENT 'True status of each road segment; new row per change';

CREATE TABLE IF NOT EXISTS knighthacks.truth.truth_people (
    event_id          STRING NOT NULL COMMENT 'Unique per truth change; dedupe key',
    id                STRING NOT NULL COMMENT 'Stable person-group id',
    scenario_id       STRING NOT NULL,
    building_id       STRING,
    count             INT COMMENT 'Number of people in the group',
    condition         STRING,
    mobility          STRING,
    language          STRING,
    simulation_time_s DOUBLE
) COMMENT 'True people groups (trapped/injured/etc.); new row per change';

-- event_id is not in final-database.md for truth_links; added so every truth table dedupes the same way.
CREATE TABLE IF NOT EXISTS knighthacks.truth.truth_links (
    event_id                STRING NOT NULL COMMENT 'Unique per link; dedupe key',
    scenario_id             STRING NOT NULL,
    observation_id          STRING NOT NULL COMMENT 'Neon ops.observations.id',
    truth_entity_type       STRING COMMENT 'building / road / people; NULL for rumors and chatter',
    truth_entity_id         STRING,
    truth_simulation_time_s DOUBLE,
    match_type              STRING COMMENT 'matched / misleading / fabricated / unmatched'
) COMMENT 'Links each generated report to the truth it describes (or marks it as rumor)';

-- Allowed values. A wrong value from the simulator fails loudly instead of corrupting scores.
-- (Delta has no ADD CONSTRAINT IF NOT EXISTS; these error harmlessly if they already exist.)
ALTER TABLE knighthacks.truth.truth_building_damage ADD CONSTRAINT damage_level_valid
    CHECK (damage_level IN ('none', 'light', 'moderate', 'severe', 'collapsed'));
ALTER TABLE knighthacks.truth.truth_links ADD CONSTRAINT match_type_valid
    CHECK (match_type IN ('matched', 'misleading', 'fabricated', 'unmatched'));
-- NULL allowed: rumors (fabricated) and chatter (unmatched) have no real entity behind them.
ALTER TABLE knighthacks.truth.truth_links ADD CONSTRAINT entity_type_valid
    CHECK (truth_entity_type IS NULL OR truth_entity_type IN ('building', 'road', 'people'));

-- ============================================================================
-- history: what the agents did (copied from Neon after each run)
-- Same columns as Neon ops.operational_events in database/schema.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS knighthacks.history.simulation_events (
    event_id   STRING NOT NULL COMMENT 'Neon ops.operational_events.event_id; unique, dedupe key',
    run_id     STRING COMMENT 'Run id, equals ops.simulation_runs.id and truth.scenarios.id',
    table_name STRING NOT NULL COMMENT 'Neon ops table that changed, e.g. incidents, decisions',
    row_id     STRING COMMENT 'Id of the row that changed in that table',
    payload    STRING COMMENT 'JSON of the row after the change',
    created_at TIMESTAMP NOT NULL COMMENT 'When the change happened'
) COMMENT 'Diary of everything agents and operators did during a run, copied from Neon ops.operational_events after it ends';

-- ============================================================================
-- eval: the grades ("Plus: run scorecards" in final-database.md)
-- One row per (run, metric), so new metrics need no schema change.
-- ============================================================================

CREATE TABLE IF NOT EXISTS knighthacks.eval.scorecards (
    scenario_id  STRING NOT NULL,
    metric       STRING NOT NULL COMMENT 'rescue_found_rate, rescue_people_found, incident_precision, rumors_acted_on, rumors_caught, duplicate_incidents, road_block_recall, road_block_precision (databricks/scorecard.sql)',
    value        DOUBLE,
    details      STRING COMMENT 'JSON with counts behind the metric',
    agent_config STRING COMMENT 'Setup being scored: the agents'' model (and version), e.g. qwen3-vl-8b-instruct',
    scored_at    TIMESTAMP
) COMMENT 'Scores for each run: agent beliefs vs ground truth';
