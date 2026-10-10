/** JSON-compatible values returned by Neon jsonb columns and Databricks JSON fields. */
export type JsonPrimitive = string | number | boolean | null;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

/** API GeoJSON representation of PostGIS geometries. All stored geometries use SRID 4326. */
export interface PointGeometry {
  type: "Point";
  coordinates: [longitude: number, latitude: number];
}

export interface LineStringGeometry {
  type: "LineString";
  coordinates: [longitude: number, latitude: number][];
}

export interface PolygonGeometry {
  type: "Polygon";
  coordinates: [longitude: number, latitude: number][][];
}

export type Timestamp = string;
export type Uuid = string;
/** SQL bigint values are represented as strings in JSON APIs to preserve precision. */
export type Int64 = string;

// static schema (read-only to agents)
export interface MapVersion {
  id: string;
  created_at: Timestamp;
  source_versions: JsonObject;
  notes: string;
}

export type BuildingEra = "pre1971" | "1971_1981" | "post1981";

export interface Building {
  map_version_id: string;
  id: string;
  geom: PolygonGeometry;
  height_m: number;
  height_source: string;
  floors: number;
  floors_source: string;
  basement_floors: number;
  footprint_m2: number;
  structure: string;
  structure_source: string;
  structure_basis: string;
  fire_resistance: string | null;
  fireproof_code: string | null;
  year_built: number;
  year_source: string;
  era: BuildingEra;
  era_probs: JsonObject;
  usage: string;
  usage_code: string;
  usage_source: string;
  zoning: string;
  zoning_ja: string;
  soil_amp: number | null;
  name_en: string;
  name_ja: string;
  address_en: string;
  address_ja: string;
  facility_id: string | null;
  facility_type: string | null;
  facility_subtype: string | null;
  facility_name_en: string | null;
  map_id: string;
  plateau_building_id: string;
}

export interface RoadSegment {
  map_version_id: string;
  id: string;
  geom: LineStringGeometry;
  from_node: Int64;
  to_node: Int64;
  length_m: number;
  speed_kmh: number;
  oneway: boolean;
  name_en: string;
  name_ja: string;
}

export type FacilityType = "hospital" | "fire_station" | "police" | "shelter";

export interface Facility {
  map_version_id: string;
  id: string;
  type: FacilityType;
  name_en: string;
  name_ja: string;
  geom: PointGeometry;
  building_id: string;
  capacity: number | null;
  access_node: Int64;
  subtype: string;
  building_match: "inside" | "nearest";
  source: string;
  note: string;
}

// ops schema (agent and operator-facing records)
export type ObservationChannel =
  | "emergency_call_119"
  | "eyewitness_text"
  | "social_media"
  | "fire_police_radio"
  | "hospital_feed"
  | "shelter_update"
  | "ward_bulletin"
  | "sensor"
  | "responder_update";

export type ObservationFormat = "audio" | "text" | "structured";
export type LocationPrecision = "gps" | "address" | "landmark" | "vague";

export interface SimulationRun {
  id: Uuid;
  map_version_id: string;
  seed: Int64;
  hazard_types: string[];
  status: string;
  started_at: Timestamp;
  ended_at: Timestamp | null;
}

export interface Observation {
  id: Uuid;
  run_id: Uuid;
  channel: ObservationChannel;
  source_reliability: number;
  format: ObservationFormat;
  lang: string;
  original_text: string;
  text_en: string | null;
  media_url: string | null;
  claimed_location: string;
  claimed_geom: PointGeometry | null;
  location_precision: LocationPrecision;
  received_at: Timestamp;
  sim_time_s: number;
}

export type IncidentStatus =
  "reported" | "corroborated" | "disputed" | "resolved";

export interface PersonGroup {
  group_id: string;
  est_count: number;
  condition: string;
  needs: string[];
  confidence: number;
}

export interface IncidentEvidence {
  supports: Uuid[];
  contradicts: Uuid[];
}

export interface Incident {
  id: Uuid;
  run_id: Uuid;
  geom: PointGeometry;
  uncertainty_m: number;
  status: IncidentStatus;
  building_id: string | null;
  person_groups: PersonGroup[];
  evidence: IncidentEvidence;
  created_at: Timestamp;
  updated_at: Timestamp;
}

export type RoadRestrictionStatus = "OPEN" | "LIMITED" | "BLOCKED";

export interface RoadRestriction {
  run_id: Uuid;
  road_id: string;
  status: RoadRestrictionStatus;
  source_observation_id: Uuid;
  updated_at: Timestamp;
}

export type ResourceStatus =
  "available" | "en_route" | "busy" | "out_of_service";

export interface Resource {
  id: Uuid;
  run_id: Uuid;
  type: string;
  name: string;
  geom: PointGeometry;
  home_facility_id: string;
  status: ResourceStatus;
  capacity: number;
  skills: string[];
}

export type DecisionStatus = "draft" | "approved" | "invalidated" | "sent";

export interface Decision {
  id: Uuid;
  run_id: Uuid;
  incident_id: Uuid;
  version: number;
  assignments: JsonValue;
  route_geojson: JsonValue | null;
  fallback_route_geojson: JsonValue | null;
  eta_s: number | null;
  status: DecisionStatus;
  invalidated_reason: string | null;
  approved_by: string | null;
  approved_at: Timestamp | null;
}

/**
 * A decision row represents a versioned response plan. The agreed v1 schema
 * has no separate plan_versions table.
 */
export type PlanVersion = Decision;

export interface AgentRun {
  id: Uuid;
  run_id: Uuid;
  agent: string;
  input_ref: string;
  output: JsonValue;
  duration_ms: number;
  created_at: Timestamp;
}

export interface Notification {
  id: Uuid;
  run_id: Uuid;
  decision_id: Uuid;
  decision_version: number;
  recipient: string;
  sent_at: Timestamp;
  acked_at: Timestamp | null;
}

export interface OperationalEvent {
  event_id: Uuid;
  run_id: Uuid;
  table_name: string;
  row_id: string;
  payload: JsonObject;
  created_at: Timestamp;
}

// Databricks models (read through the simulator/reveal-truth API, never by agents).
export interface Scenario {
  id: string;
  seed: Int64;
  hazard_types: string[];
  params: string;
  map_version_id: string;
  simulator_version: string;
  started_at: Timestamp;
  ended_at: Timestamp | null;
}

export interface TruthBuildingDamage {
  event_id: string;
  scenario_id: string;
  building_id: string;
  intensity: number;
  damage_level: "none" | "light" | "moderate" | "severe" | "collapsed";
  simulation_time_s: number;
  sampled_attributes: string;
}

export interface TruthRoadStatus {
  event_id: string;
  scenario_id: string;
  road_id: string;
  status: string;
  cause: string;
  simulation_time_s: number;
}

export interface TruthPerson {
  event_id: string;
  id: string;
  scenario_id: string;
  building_id: string;
  count: number;
  condition: string;
  mobility: string;
  language: string;
  simulation_time_s: number;
}

export interface TruthLink {
  scenario_id: string;
  observation_id: Uuid;
  truth_entity_type: "building" | "road" | "people";
  truth_entity_id: string;
  truth_simulation_time_s: number;
  match_type: "matched" | "misleading" | "fabricated" | "unmatched";
}

export interface SimulationEvent {
  event_id: string;
  run_id: Uuid;
  table_name: string;
  row_id: string;
  payload: JsonObject;
  created_at: Timestamp;
}

/** UI-only projections, composed from schema records rather than stored as tables. */
export type ObservationCategory = "Radio" | "Social" | "Drone" | "Other";

export interface ObservationFeedItem {
  observation: Observation;
  category: ObservationCategory;
  incident: Incident | null;
}

export interface DispatchCardModel {
  resource: Resource;
  decision: Decision | null;
  destination: string;
  routeLabel: string;
}
