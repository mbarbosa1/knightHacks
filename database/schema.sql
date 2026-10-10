-- Neon Postgres + PostGIS schema, matching database/final-database.md (v1).
-- Run once as the database owner:  psql "$DATABASE_URL" -f database/schema.sql
-- Safe to re-run: everything is IF NOT EXISTS / idempotent.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS static;
CREATE SCHEMA IF NOT EXISTS ops;

-- ============================================================================
-- static: the map (agents read-only, maintained by map_import)
-- ============================================================================

CREATE TABLE IF NOT EXISTS static.map_versions (
    id              text PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    source_versions jsonb,          -- PLATEAU / OSM / J-SHIS versions
    notes           text
);

CREATE TABLE IF NOT EXISTS static.buildings (
    map_version_id      text NOT NULL REFERENCES static.map_versions(id) ON DELETE CASCADE,
    id                  text NOT NULL,                 -- bldg_<uuid>
    geom                geometry(Polygon, 4326) NOT NULL,
    height_m            real,
    height_source       text,                          -- PLATEAU / inferred
    floors              int,
    floors_source       text,
    basement_floors     int,
    footprint_m2        real,
    structure           text CHECK (structure IN ('wood', 'rc', 'steel', 'light_steel')),
    structure_source    text,
    structure_basis     text,
    fire_resistance     text,
    fireproof_code      text,
    year_built          int,
    year_source         text,
    era                 text CHECK (era IN ('pre1971', '1971_1981', 'post1981')),
    era_probs           jsonb,
    usage               text,
    usage_code          text,
    usage_source        text,
    zoning              text,
    zoning_ja           text,
    soil_amp            real,                          -- J-SHIS ARV (rel. Vs=400); NULL until loaded
    name_en             text,
    name_ja             text,
    address_en          text,
    address_ja          text,
    facility_id         text,
    facility_type       text,
    facility_subtype    text,
    facility_name_en    text,
    map_id              text,                          -- id used by the map layer (not unique)
    plateau_building_id text,
    PRIMARY KEY (map_version_id, id)
);
CREATE INDEX IF NOT EXISTS buildings_geom_gix ON static.buildings USING gist (geom);

CREATE TABLE IF NOT EXISTS static.road_segments (
    map_version_id text NOT NULL REFERENCES static.map_versions(id) ON DELETE CASCADE,
    id             text NOT NULL,                      -- OSMnx "u_v_key", same as NetworkX
    geom           geometry(LineString, 4326) NOT NULL,
    from_node      bigint NOT NULL,
    to_node        bigint NOT NULL,
    length_m       real,
    speed_kmh      real,
    oneway         boolean,
    name_en        text,
    name_ja        text,
    PRIMARY KEY (map_version_id, id)
);
CREATE INDEX IF NOT EXISTS road_segments_geom_gix ON static.road_segments USING gist (geom);

CREATE TABLE IF NOT EXISTS static.facilities (
    map_version_id text NOT NULL REFERENCES static.map_versions(id) ON DELETE CASCADE,
    id             text NOT NULL,
    type           text NOT NULL CHECK (type IN ('hospital', 'fire_station', 'police', 'shelter')),
    name_en        text,
    name_ja        text,
    geom           geometry(Point, 4326) NOT NULL,
    building_id    text,
    capacity       int,                                -- NULL = unknown
    access_node    bigint,                             -- nearest road graph node
    subtype        text,                               -- hospital / branch / police_box / school / open_space
    building_match text CHECK (building_match IN ('inside', 'nearest')),
    source         text,
    note           text,
    PRIMARY KEY (map_version_id, id)
);
CREATE INDEX IF NOT EXISTS facilities_geom_gix ON static.facilities USING gist (geom);

-- buildings <-> facilities reference each other; deferred so one transaction can load both.
DO $$ BEGIN
    ALTER TABLE static.facilities ADD CONSTRAINT facilities_building_fk
        FOREIGN KEY (map_version_id, building_id) REFERENCES static.buildings(map_version_id, id)
        DEFERRABLE INITIALLY DEFERRED;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE static.buildings ADD CONSTRAINT buildings_facility_fk
        FOREIGN KEY (map_version_id, facility_id) REFERENCES static.facilities(map_version_id, id)
        DEFERRABLE INITIALLY DEFERRED;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================================
-- ops: live run state (agents read/write)
-- ============================================================================

DO $$ BEGIN
    CREATE TYPE ops.observation_channel AS ENUM (
        'emergency_call_119', 'eyewitness_text', 'social_media', 'fire_police_radio',
        'hospital_feed', 'shelter_update', 'ward_bulletin', 'sensor', 'responder_update');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS ops.simulation_runs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),   -- = Databricks scenarios.id
    map_version_id text NOT NULL REFERENCES static.map_versions(id),
    seed           bigint NOT NULL,
    hazard_types   text[] NOT NULL,
    status         text,
    started_at     timestamptz,
    ended_at       timestamptz
);

CREATE TABLE IF NOT EXISTS ops.observations (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id             uuid NOT NULL REFERENCES ops.simulation_runs(id) ON DELETE CASCADE,
    channel            ops.observation_channel NOT NULL,
    source_reliability real CHECK (source_reliability BETWEEN 0 AND 1),
    format             text CHECK (format IN ('audio', 'text', 'structured')),
    lang               text,
    original_text      text,
    text_en            text,
    media_url          text,                               -- S3 URL
    claimed_location   text,
    claimed_geom       geometry(Point, 4326),
    location_precision text CHECK (location_precision IN ('gps', 'address', 'landmark', 'vague')),
    received_at        timestamptz NOT NULL DEFAULT now(),
    sim_time_s         real
);
CREATE INDEX IF NOT EXISTS observations_run_idx ON ops.observations (run_id, received_at);
CREATE INDEX IF NOT EXISTS observations_geom_gix ON ops.observations USING gist (claimed_geom);

CREATE TABLE IF NOT EXISTS ops.incidents (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        uuid NOT NULL REFERENCES ops.simulation_runs(id) ON DELETE CASCADE,
    geom          geometry(Point, 4326),
    uncertainty_m real,
    status        text CHECK (status IN ('reported', 'corroborated', 'disputed', 'resolved')),
    building_id   text,             -- checked against static.buildings for the run's map version
    person_groups jsonb,            -- [{group_id, est_count, condition, needs, confidence}]
    evidence      jsonb,            -- {supports: [obs ids], contradicts: [obs ids]}
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incidents_run_idx ON ops.incidents (run_id);
CREATE INDEX IF NOT EXISTS incidents_geom_gix ON ops.incidents USING gist (geom);

CREATE TABLE IF NOT EXISTS ops.road_restrictions (
    run_id                uuid NOT NULL REFERENCES ops.simulation_runs(id) ON DELETE CASCADE,
    road_id               text NOT NULL,   -- checked against static.road_segments for the run's map version
    status                text NOT NULL CHECK (status IN ('OPEN', 'LIMITED', 'BLOCKED')),
    source_observation_id uuid REFERENCES ops.observations(id),
    updated_at            timestamptz NOT NULL DEFAULT now(),   -- change triggers reroute
    PRIMARY KEY (run_id, road_id)
);

CREATE TABLE IF NOT EXISTS ops.resources (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id           uuid NOT NULL REFERENCES ops.simulation_runs(id) ON DELETE CASCADE,
    type             text,
    name             text,
    geom             geometry(Point, 4326),
    home_facility_id text,             -- checked against static.facilities for the run's map version
    status           text CHECK (status IN ('available', 'en_route', 'busy', 'out_of_service')),
    capacity         int,
    skills           text[]
);
CREATE INDEX IF NOT EXISTS resources_run_idx ON ops.resources (run_id);
CREATE INDEX IF NOT EXISTS resources_geom_gix ON ops.resources USING gist (geom);

CREATE TABLE IF NOT EXISTS ops.decisions (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                 uuid NOT NULL REFERENCES ops.simulation_runs(id) ON DELETE CASCADE,
    incident_id            uuid REFERENCES ops.incidents(id),
    version                int NOT NULL DEFAULT 1,
    assignments            jsonb,
    route_geojson          jsonb,
    fallback_route_geojson jsonb,
    eta_s                  real,
    status                 text CHECK (status IN ('draft', 'approved', 'invalidated', 'sent')),
    invalidated_reason     text,
    approved_by            text,
    approved_at            timestamptz,
    UNIQUE (incident_id, version)
);
CREATE INDEX IF NOT EXISTS decisions_run_idx ON ops.decisions (run_id);

CREATE TABLE IF NOT EXISTS ops.agent_runs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      uuid NOT NULL REFERENCES ops.simulation_runs(id) ON DELETE CASCADE,
    agent       text NOT NULL,
    input_ref   text,
    output      jsonb,
    duration_ms int,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runs_run_idx ON ops.agent_runs (run_id);

CREATE TABLE IF NOT EXISTS ops.notifications (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id           uuid NOT NULL REFERENCES ops.simulation_runs(id) ON DELETE CASCADE,
    decision_id      uuid REFERENCES ops.decisions(id),
    decision_version int,
    recipient        text,
    sent_at          timestamptz,
    acked_at         timestamptz
);

-- Outbox: every ops writer appends here; exported to Databricks after the run.
CREATE TABLE IF NOT EXISTS ops.operational_events (
    event_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id     uuid,
    table_name text NOT NULL,
    row_id     text,
    payload    jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS operational_events_run_idx ON ops.operational_events (run_id, created_at);

-- References into static carry the run's pinned map version (stored only on simulation_runs),
-- so they are enforced by trigger instead of a plain FK.
CREATE OR REPLACE FUNCTION ops.check_static_ref() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    col text := TG_ARGV[0];       -- column on NEW holding the static id
    tbl text := TG_ARGV[1];       -- static table it points to
    ref text := to_jsonb(NEW) ->> col;
    ok  boolean;
BEGIN
    IF ref IS NULL THEN RETURN NEW; END IF;
    EXECUTE format(
        'SELECT EXISTS (SELECT 1 FROM static.%I s JOIN ops.simulation_runs r
                         ON r.map_version_id = s.map_version_id
                        WHERE r.id = $1 AND s.id = $2)', tbl)
        INTO ok USING NEW.run_id, ref;
    IF NOT ok THEN
        RAISE EXCEPTION 'ops.%.%: % not found in static.% for run %', TG_TABLE_NAME, col, ref, tbl, NEW.run_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS incidents_building_ref ON ops.incidents;
CREATE TRIGGER incidents_building_ref BEFORE INSERT OR UPDATE OF building_id ON ops.incidents
    FOR EACH ROW EXECUTE FUNCTION ops.check_static_ref('building_id', 'buildings');
DROP TRIGGER IF EXISTS road_restrictions_road_ref ON ops.road_restrictions;
CREATE TRIGGER road_restrictions_road_ref BEFORE INSERT OR UPDATE OF road_id ON ops.road_restrictions
    FOR EACH ROW EXECUTE FUNCTION ops.check_static_ref('road_id', 'road_segments');
DROP TRIGGER IF EXISTS resources_facility_ref ON ops.resources;
CREATE TRIGGER resources_facility_ref BEFORE INSERT OR UPDATE OF home_facility_id ON ops.resources
    FOR EACH ROW EXECUTE FUNCTION ops.check_static_ref('home_facility_id', 'facilities');

-- ============================================================================
-- Roles
--   map_import : maintains static (used by scripts/load_static.py)
--   simulator  : reads static, writes all of ops
--   agents     : reads static; SELECT/INSERT/UPDATE only on the ops tables it needs; owns nothing
-- Created without passwords. Set them once:  ALTER ROLE agents WITH PASSWORD '...';
-- (or create the roles in the Neon console first; this block then skips them).
-- ============================================================================

DO $$ BEGIN CREATE ROLE map_import LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE simulator  LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE agents     LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;

GRANT USAGE ON SCHEMA static TO map_import, simulator, agents;
GRANT USAGE ON SCHEMA ops TO simulator, agents;
GRANT USAGE ON SCHEMA public TO map_import, simulator, agents;   -- PostGIS functions live here

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA static TO map_import;
GRANT SELECT ON ALL TABLES IN SCHEMA static TO simulator, agents;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ops TO simulator;

GRANT SELECT ON ops.simulation_runs, ops.observations TO agents;
GRANT SELECT, INSERT, UPDATE ON ops.incidents, ops.decisions, ops.agent_runs, ops.notifications,
                                ops.road_restrictions, ops.resources TO agents;
GRANT INSERT ON ops.operational_events TO agents;

REVOKE CREATE ON SCHEMA static, ops, public FROM agents;
