-- KnightHacks / main / neondb
-- Requires existing PostGIS (public), "staticMapData", and ops schemas.
-- Additive initial table migration; stops if a target table already exists.
-- No data import, credentials, grants, or ground-truth tables.
'''
RECORD - DONT RUNNNNN PLS
Created all 13 tables, plus triggers that automatically log every write to operational_events.
'''
BEGIN;

CREATE TABLE "staticMapData".map_versions (
 id text PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(),
 source_versions jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(source_versions)='object'),
 notes text
);

CREATE TABLE "staticMapData".buildings (
 map_version_id text NOT NULL REFERENCES "staticMapData".map_versions(id),
 id text NOT NULL,
 map_id text,
 geom public.geometry(Geometry,4326) NOT NULL CHECK (public.GeometryType(geom) IN ('POLYGON','MULTIPOLYGON')),
 height_m numeric CHECK(height_m>=0),
 floors integer CHECK(floors>=0),
 structure text,
 structure_source text CHECK(structure_source IN ('PLATEAU','inferred')),
 structure_basis text,
 fire_resistance text,
 fireproof_code text,
 year_built integer CHECK(year_built>0),
 year_source text CHECK(year_source IN ('PLATEAU','inferred')),
 era_probs jsonb CHECK(jsonb_typeof(era_probs) IN ('object','array')),
 usage text,
 usage_code text,
 soil_amp numeric CHECK(soil_amp>0),
 facility_type text CHECK(facility_type IN ('hospital','shelter','fire_station','police')),
 facility_id text,
 name_en text,
 name_ja text,
 address_en text,
 address_ja text,
 PRIMARY KEY(map_version_id,id)
);

CREATE TABLE "staticMapData".road_segments (
 map_version_id text NOT NULL REFERENCES "staticMapData".map_versions(id),
 id text NOT NULL,
 geom public.geometry(Geometry,4326) NOT NULL CHECK(public.GeometryType(geom) IN ('LINESTRING','MULTILINESTRING')),
 from_node bigint NOT NULL,
 to_node bigint NOT NULL,
 length_m numeric CHECK(length_m>=0),
 speed_kmh numeric CHECK(speed_kmh>=0),
 oneway boolean,
 name_en text,
 name_ja text,
 PRIMARY KEY(map_version_id,id)
);

CREATE TABLE "staticMapData".facilities (
 map_version_id text NOT NULL REFERENCES "staticMapData".map_versions(id),
 id text NOT NULL,
 type text NOT NULL CHECK(type IN ('hospital','shelter','fire_station','police')),
 name_en text,
 name_ja text,
 geom public.geometry(Point,4326) NOT NULL,
 building_id text,
 capacity integer CHECK(capacity>=0),
 PRIMARY KEY(map_version_id,id),
 FOREIGN KEY(map_version_id,building_id) REFERENCES "staticMapData".buildings(map_version_id,id)
);

ALTER TABLE "staticMapData".buildings ADD CONSTRAINT buildings_facility_fk
 FOREIGN KEY(map_version_id,facility_id) REFERENCES "staticMapData".facilities(map_version_id,id)
 DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE ops.simulation_runs (
 id text PRIMARY KEY,
 seed bigint NOT NULL,
 hazard_types text[] NOT NULL CHECK(cardinality(hazard_types)>0 AND hazard_types <@ ARRAY['earthquake','flood']::text[]),
 parameters jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(parameters)='object'),
 map_version_id text NOT NULL REFERENCES "staticMapData".map_versions(id),
 road_graph_version text NOT NULL,
 simulator_version text,
 simulation_time_s numeric NOT NULL DEFAULT 0 CHECK(simulation_time_s>=0),
 started_at timestamptz NOT NULL DEFAULT now(),
 ended_at timestamptz,
 status text NOT NULL DEFAULT 'created' CHECK(status IN ('created','running','completed','failed')),
 export_status text NOT NULL DEFAULT 'pending' CHECK(export_status IN ('pending','exporting','completed','failed')),
 exported_at timestamptz,
 UNIQUE(id,map_version_id),
 CHECK(ended_at IS NULL OR ended_at>=started_at)
);

CREATE TABLE ops.observations (
 run_id text NOT NULL REFERENCES ops.simulation_runs(id),
 id text NOT NULL,
 channel text NOT NULL CHECK(channel IN ('emergency_call_119','eyewitness_text','social_media','fire_police_radio','hospital_feed','shelter_update','ward_bulletin','sensor','responder_update')),
 format text CHECK(format IN ('audio','text','structured')),
 original_text text,
 language text,
 text_en text,
 media_url text,
 claimed_location text,
 location public.geometry(Point,4326),
 location_precision text CHECK(location_precision IN ('gps','address','landmark','vague')),
 source_reliability numeric CHECK(source_reliability BETWEEN 0 AND 1),
 received_at timestamptz NOT NULL DEFAULT now(),
 simulation_time_s numeric NOT NULL CHECK(simulation_time_s>=0),
 PRIMARY KEY(run_id,id)
);

CREATE TABLE ops.incidents (
 run_id text NOT NULL,
 id text NOT NULL,
 map_version_id text NOT NULL,
 location public.geometry(Point,4326),
 building_id text,
 location_uncertainty_m numeric CHECK(location_uncertainty_m>=0),
 status text NOT NULL DEFAULT 'reported' CHECK(status IN ('reported','corroborated','disputed','resolved')),
 confidence numeric CHECK(confidence BETWEEN 0 AND 1),
 person_groups jsonb CHECK(jsonb_typeof(person_groups)='array'),
 evidence jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(evidence)='array'),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 simulation_time_s numeric NOT NULL CHECK(simulation_time_s>=0),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,map_version_id) REFERENCES ops.simulation_runs(id,map_version_id),
 FOREIGN KEY(map_version_id,building_id) REFERENCES "staticMapData".buildings(map_version_id,id),
 CHECK(updated_at>=created_at)
);

CREATE TABLE ops.road_restrictions (
 run_id text NOT NULL,
 map_version_id text NOT NULL,
 road_id text NOT NULL,
 status text NOT NULL CHECK(status IN ('OPEN','LIMITED','BLOCKED')),
 cause text,
 source_observation_id text,
 updated_at timestamptz NOT NULL DEFAULT now(),
 simulation_time_s numeric NOT NULL CHECK(simulation_time_s>=0),
 PRIMARY KEY(run_id,road_id),
 FOREIGN KEY(run_id,map_version_id) REFERENCES ops.simulation_runs(id,map_version_id),
 FOREIGN KEY(map_version_id,road_id) REFERENCES "staticMapData".road_segments(map_version_id,id),
 FOREIGN KEY(run_id,source_observation_id) REFERENCES ops.observations(run_id,id)
);

CREATE TABLE ops.resources (
 run_id text NOT NULL REFERENCES ops.simulation_runs(id),
 id text NOT NULL,
 type text NOT NULL,
 name text,
 location public.geometry(Point,4326),
 status text NOT NULL DEFAULT 'available' CHECK(status IN ('available','en_route','busy','out_of_service')),
 capacity integer CHECK(capacity>=0),
 skills text[],
 updated_at timestamptz NOT NULL DEFAULT now(),
 simulation_time_s numeric NOT NULL CHECK(simulation_time_s>=0),
 PRIMARY KEY(run_id,id)
);

CREATE TABLE ops.decisions (
 run_id text NOT NULL,
 id text NOT NULL,
 incident_id text NOT NULL,
 version integer NOT NULL CHECK(version>0),
 resource_assignments jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(resource_assignments)='array'),
 route jsonb CHECK(jsonb_typeof(route)='object'),
 fallback_route jsonb CHECK(jsonb_typeof(fallback_route)='object'),
 eta_seconds integer CHECK(eta_seconds>=0),
 reason text,
 status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved','invalidated','sent')),
 approved_by text,
 approved_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 simulation_time_s numeric NOT NULL CHECK(simulation_time_s>=0),
 PRIMARY KEY(run_id,id,version),
 FOREIGN KEY(run_id,incident_id) REFERENCES ops.incidents(run_id,id),
 CHECK((approved_by IS NULL)=(approved_at IS NULL)),
 CHECK(status NOT IN ('approved','sent') OR approved_at IS NOT NULL)
);

CREATE TABLE ops.agent_runs (
 run_id text NOT NULL REFERENCES ops.simulation_runs(id),
 id text NOT NULL,
 agent_name text NOT NULL,
 input_refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(input_refs)='array'),
 output jsonb,
 duration_ms integer CHECK(duration_ms>=0),
 created_at timestamptz NOT NULL DEFAULT now(),
 simulation_time_s numeric NOT NULL CHECK(simulation_time_s>=0),
 PRIMARY KEY(run_id,id)
);

CREATE TABLE ops.notifications (
 run_id text NOT NULL,
 id text NOT NULL,
 decision_id text NOT NULL,
 decision_version integer NOT NULL,
 recipient text NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','acknowledged','failed')),
 sent_at timestamptz,
 acknowledged_at timestamptz,
 simulation_time_s numeric NOT NULL CHECK(simulation_time_s>=0),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,decision_id,decision_version) REFERENCES ops.decisions(run_id,id,version),
 CHECK(status NOT IN ('sent','acknowledged') OR sent_at IS NOT NULL),
 CHECK(status<>'acknowledged' OR acknowledged_at IS NOT NULL),
 CHECK(acknowledged_at IS NULL OR (sent_at IS NOT NULL AND acknowledged_at>=sent_at))
);

CREATE TABLE ops.operational_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 run_id text NOT NULL REFERENCES ops.simulation_runs(id),
 entity_type text NOT NULL,
 entity_id text NOT NULL,
 event_type text NOT NULL CHECK(event_type IN ('INSERT','UPDATE','DELETE')),
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 simulation_time_s numeric NOT NULL CHECK(simulation_time_s>=0)
);

CREATE INDEX buildings_geom_gist ON "staticMapData".buildings USING gist (geom);

CREATE INDEX road_segments_geom_gist ON "staticMapData".road_segments USING gist (geom);

CREATE INDEX facilities_geom_gist ON "staticMapData".facilities USING gist (geom);

CREATE INDEX observations_location_gist ON "ops".observations USING gist (location);

CREATE INDEX incidents_location_gist ON "ops".incidents USING gist (location);

CREATE INDEX resources_location_gist ON "ops".resources USING gist (location);

CREATE INDEX buildings_facility_idx ON "staticMapData".buildings(map_version_id,facility_id) WHERE facility_id IS NOT NULL;

CREATE INDEX facilities_building_idx ON "staticMapData".facilities(map_version_id,building_id) WHERE building_id IS NOT NULL;

CREATE INDEX facilities_type_idx ON "staticMapData".facilities(map_version_id,type);

CREATE INDEX observations_time_idx ON ops.observations(run_id,simulation_time_s);

CREATE INDEX incidents_building_idx ON ops.incidents(map_version_id,building_id) WHERE building_id IS NOT NULL;

CREATE INDEX incidents_status_idx ON ops.incidents(run_id,status);

CREATE INDEX roads_map_idx ON ops.road_restrictions(map_version_id,road_id);

CREATE INDEX roads_observation_idx ON ops.road_restrictions(run_id,source_observation_id) WHERE source_observation_id IS NOT NULL;

CREATE INDEX resources_status_idx ON ops.resources(run_id,status,type);

CREATE INDEX decisions_incident_idx ON ops.decisions(run_id,incident_id,created_at);

CREATE INDEX notifications_decision_idx ON ops.notifications(run_id,decision_id,decision_version);

CREATE INDEX agent_runs_time_idx ON ops.agent_runs(run_id,simulation_time_s);

CREATE INDEX operational_events_export_idx ON ops.operational_events(run_id,occurred_at,id);

CREATE FUNCTION ops.record_operational_event() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $body$
DECLARE
 row_data jsonb;
 prior_data jsonb;
 event_run_id text;
 event_entity_id text;
 event_sim_time numeric;
 event_payload jsonb;
BEGIN
 IF TG_OP='DELETE' THEN row_data:=to_jsonb(OLD);
 ELSE row_data:=to_jsonb(NEW); END IF;
 IF TG_OP='UPDATE' THEN prior_data:=to_jsonb(OLD); END IF;
 IF TG_TABLE_NAME='simulation_runs' THEN
  event_run_id:=row_data->>'id';
  event_sim_time:=(row_data->>'simulation_time_s')::numeric;
 ELSE
  event_run_id:=row_data->>'run_id';
  event_sim_time:=(row_data->>'simulation_time_s')::numeric;
 END IF;
 event_entity_id:=COALESCE(row_data->>'id',row_data->>'road_id');
 IF TG_TABLE_NAME='decisions' THEN
  event_entity_id:=event_entity_id||':'||(row_data->>'version');
 END IF;
 event_payload:=jsonb_build_object('row',row_data,'before',prior_data);
 INSERT INTO ops.operational_events(run_id,entity_type,entity_id,event_type,payload,simulation_time_s)
 VALUES(event_run_id,TG_TABLE_NAME,event_entity_id,TG_OP,event_payload,event_sim_time);
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$body$;

CREATE TRIGGER simulation_runs_event_log AFTER INSERT OR UPDATE OR DELETE ON ops.simulation_runs FOR EACH ROW EXECUTE FUNCTION ops.record_operational_event();

CREATE TRIGGER observations_event_log AFTER INSERT OR UPDATE OR DELETE ON ops.observations FOR EACH ROW EXECUTE FUNCTION ops.record_operational_event();

CREATE TRIGGER incidents_event_log AFTER INSERT OR UPDATE OR DELETE ON ops.incidents FOR EACH ROW EXECUTE FUNCTION ops.record_operational_event();

CREATE TRIGGER road_restrictions_event_log AFTER INSERT OR UPDATE OR DELETE ON ops.road_restrictions FOR EACH ROW EXECUTE FUNCTION ops.record_operational_event();

CREATE TRIGGER resources_event_log AFTER INSERT OR UPDATE OR DELETE ON ops.resources FOR EACH ROW EXECUTE FUNCTION ops.record_operational_event();

CREATE TRIGGER decisions_event_log AFTER INSERT OR UPDATE OR DELETE ON ops.decisions FOR EACH ROW EXECUTE FUNCTION ops.record_operational_event();

CREATE TRIGGER agent_runs_event_log AFTER INSERT OR UPDATE OR DELETE ON ops.agent_runs FOR EACH ROW EXECUTE FUNCTION ops.record_operational_event();

CREATE TRIGGER notifications_event_log AFTER INSERT OR UPDATE OR DELETE ON ops.notifications FOR EACH ROW EXECUTE FUNCTION ops.record_operational_event();

COMMENT ON COLUMN "staticMapData".buildings.map_id IS 'Demo map identifier stored as text to preserve source IDs; validate against buildings_table.geojson during import.';

COMMENT ON COLUMN "staticMapData".buildings.era_probs IS 'Original per-building construction-era probability distribution; simulator samples each run. Validate era labels, nonnegative weights and sum during import.';

COMMENT ON COLUMN "staticMapData".buildings.year_built IS 'Reference estimate; not a replacement for era_probs. Per-run sampled era belongs in Databricks ground truth.';

COMMENT ON COLUMN ops.incidents.person_groups IS 'Array of group objects: estimated count, condition, mobility, language, needs and optional confidence. NULL means unassessed; [] means assessed with no known groups.';

COMMENT ON COLUMN ops.incidents.evidence IS 'Array of objects with observation_id and relation (supports, contradicts, duplicate); backend validates same-run observation references.';

COMMENT ON COLUMN ops.decisions.resource_assignments IS 'Array of assignment objects referencing resources in the same run; backend validates IDs and capacity.';

COMMENT ON COLUMN ops.decisions.route IS 'Chosen route as GeoJSON; longitude/latitude coordinates, EPSG:4326.';

COMMENT ON TABLE ops.operational_events IS 'Transactional change history for end-of-run export to Databricks. Export using stable UUID IDs and idempotent merge. No hidden ground truth belongs here.';

COMMENT ON COLUMN ops.operational_events.simulation_time_s IS 'Seconds from simulation start; writers must update the entity simulation_time_s on every simulation-state change.';

CREATE INDEX buildings_map_id_idx ON "staticMapData".buildings(map_version_id,map_id);

COMMIT;

