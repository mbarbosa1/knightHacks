import type {
  AgentRun,
  Building,
  Decision,
  Facility,
  Incident,
  MapVersion,
  Notification,
  Observation,
  OperationalEvent,
  Resource,
  RoadRestriction,
  RoadSegment,
  Scenario,
  SimulationRun,
  SimulationEvent,
  TruthBuildingDamage,
  TruthLink,
  TruthPerson,
  TruthRoadStatus,
} from "../types/database";

export const MOCK_RUN_ID = "11111111-1111-4111-8111-111111111111";
export const MOCK_MAP_VERSION_ID = "tokyo-plateau-2026-09";
export const MOCK_INCIDENT_ID = "22222222-2222-4222-8222-222222222042";

export const mockMapVersion: MapVersion = {
  id: MOCK_MAP_VERSION_ID,
  created_at: "2026-09-23T00:00:00.000Z",
  source_versions: {
    PLATEAU: "Tokyo 23 wards 2025",
    OSM: "2026-09-23",
    "J-SHIS": "v3.2",
  },
  notes:
    "Tokyo demonstration area; static map records are read-only to agents.",
};

export const mockBuildings: Building[] = [
  {
    map_version_id: MOCK_MAP_VERSION_ID,
    id: "bldg_shinjuku_001",
    geom: {
      type: "Polygon",
      coordinates: [
        [
          [139.6917, 35.6895],
          [139.692, 35.6895],
          [139.692, 35.6897],
          [139.6917, 35.6897],
          [139.6917, 35.6895],
        ],
      ],
    },
    height_m: 48.2,
    height_source: "PLATEAU",
    floors: 12,
    floors_source: "PLATEAU",
    basement_floors: 2,
    footprint_m2: 1840,
    structure: "rc",
    structure_source: "PLATEAU",
    structure_basis: "PLATEAU structure attribute",
    fire_resistance: "fire-resistant",
    fireproof_code: "耐火建築物",
    year_built: 1998,
    year_source: "PLATEAU",
    era: "post1981",
    era_probs: { pre1971: 0.02, "1971_1981": 0.03, post1981: 0.95 },
    usage: "government",
    usage_code: "office",
    usage_source: "PLATEAU",
    zoning: "commercial",
    zoning_ja: "商業地域",
    soil_amp: null,
    name_en: "Shinjuku Ward Office",
    name_ja: "新宿区役所",
    address_en: "1-4-1 Kabukicho, Shinjuku City, Tokyo",
    address_ja: "東京都新宿区歌舞伎町一丁目4番1号",
    facility_id: "facility_shinjuku_fire",
    facility_type: "fire_station",
    facility_subtype: "branch",
    facility_name_en: "Shinjuku Fire Station",
    map_id: "map-shinjuku-001",
    plateau_building_id: "PLATEAU_13104-bldg-0001",
  },
  {
    map_version_id: MOCK_MAP_VERSION_ID,
    id: "bldg_shinjuku_002",
    geom: {
      type: "Polygon",
      coordinates: [
        [
          [139.693, 35.69],
          [139.6933, 35.69],
          [139.6933, 35.6902],
          [139.693, 35.6902],
          [139.693, 35.69],
        ],
      ],
    },
    height_m: 22.5,
    height_source: "PLATEAU",
    floors: 7,
    floors_source: "PLATEAU",
    basement_floors: 1,
    footprint_m2: 920,
    structure: "steel",
    structure_source: "PLATEAU",
    structure_basis: "PLATEAU structure attribute",
    fire_resistance: null,
    fireproof_code: null,
    year_built: 1986,
    year_source: "inferred",
    era: "post1981",
    era_probs: { pre1971: 0.04, "1971_1981": 0.08, post1981: 0.88 },
    usage: "residential",
    usage_code: "共同住宅",
    usage_source: "PLATEAU",
    zoning: "commercial",
    zoning_ja: "商業地域",
    soil_amp: null,
    name_en: "Kabukicho Residences",
    name_ja: "歌舞伎町レジデンス",
    address_en: "Kabukicho, Shinjuku City, Tokyo",
    address_ja: "東京都新宿区歌舞伎町",
    facility_id: null,
    facility_type: null,
    facility_subtype: null,
    facility_name_en: null,
    map_id: "map-shinjuku-002",
    plateau_building_id: "PLATEAU_13104-bldg-0002",
  },
];

export const mockRoadSegments: RoadSegment[] = [
  {
    map_version_id: MOCK_MAP_VERSION_ID,
    id: "road_1001_1002",
    geom: {
      type: "LineString",
      coordinates: [
        [139.6915, 35.6894],
        [139.6922, 35.6898],
      ],
    },
    from_node: "1001",
    to_node: "1002",
    length_m: 82.4,
    speed_kmh: 30,
    oneway: false,
    name_en: "Yasukuni-dori",
    name_ja: "靖国通り",
  },
  {
    map_version_id: MOCK_MAP_VERSION_ID,
    id: "road_1002_1003",
    geom: {
      type: "LineString",
      coordinates: [
        [139.6922, 35.6898],
        [139.693, 35.6901],
      ],
    },
    from_node: "1002",
    to_node: "1003",
    length_m: 64.1,
    speed_kmh: 20,
    oneway: true,
    name_en: "Shokuan-dori",
    name_ja: "職安通り",
  },
];

export const mockFacilities: Facility[] = [
  {
    map_version_id: MOCK_MAP_VERSION_ID,
    id: "facility_shinjuku_fire",
    type: "fire_station",
    name_en: "Shinjuku Fire Station",
    name_ja: "新宿消防署",
    geom: { type: "Point", coordinates: [139.692, 35.6896] },
    building_id: "bldg_shinjuku_001",
    capacity: null,
    access_node: "1002",
    subtype: "branch",
    building_match: "inside",
    source: "Tokyo Fire Department facility inventory",
    note: "Demonstration facility; response capacity unknown.",
  },
];

export const mockSimulationRun: SimulationRun = {
  id: MOCK_RUN_ID,
  map_version_id: MOCK_MAP_VERSION_ID,
  seed: "20261010",
  hazard_types: ["earthquake", "tsunami"],
  status: "running",
  started_at: "2026-10-10T10:00:00.000Z",
  ended_at: null,
};

export const mockObservations: Observation[] = [
  {
    id: "33333333-3333-4333-8333-333333331001",
    run_id: MOCK_RUN_ID,
    channel: "social_media",
    source_reliability: 0.58,
    format: "text",
    lang: "ja",
    original_text: "区役所の近くで人が閉じ込められているという投稿を見ました。",
    text_en: "Someone posted that people may be trapped near the ward office.",
    media_url: null,
    claimed_location: "Shinjuku Ward Office",
    claimed_geom: { type: "Point", coordinates: [139.69185, 35.6896] },
    location_precision: "landmark",
    received_at: "2026-10-10T10:03:00.000Z",
    sim_time_s: 180,
  },
  {
    id: "33333333-3333-4333-8333-333333331002",
    run_id: MOCK_RUN_ID,
    channel: "sensor",
    source_reliability: 0.96,
    format: "structured",
    lang: "ja",
    original_text: "建物スキャン: 構造損傷を検知。要救助者の可能性。",
    text_en:
      "Building scan detected structural damage and possible occupants needing rescue.",
    media_url: "s3://tasuke-demo/observations/shinjuku-scan-01.json",
    claimed_location: "Kabukicho Residences",
    claimed_geom: { type: "Point", coordinates: [139.69315, 35.6901] },
    location_precision: "gps",
    received_at: "2026-10-10T10:02:00.000Z",
    sim_time_s: 150,
  },
  {
    id: "33333333-3333-4333-8333-333333331003",
    run_id: MOCK_RUN_ID,
    channel: "fire_police_radio",
    source_reliability: 0.91,
    format: "audio",
    lang: "ja",
    original_text: "新宿指令、区役所付近で救助要請。靖国通りは一部通行困難。",
    text_en:
      "Shinjuku command: rescue request near the ward office. Yasukuni-dori is partially restricted.",
    media_url: "s3://tasuke-demo/observations/radio-1003.wav",
    claimed_location: "Yasukuni-dori, Shinjuku",
    claimed_geom: { type: "Point", coordinates: [139.6921, 35.6897] },
    location_precision: "address",
    received_at: "2026-10-10T10:00:00.000Z",
    sim_time_s: 90,
  },
  {
    id: "33333333-3333-4333-8333-333333331004",
    run_id: MOCK_RUN_ID,
    channel: "eyewitness_text",
    source_reliability: 0.72,
    format: "text",
    lang: "ja",
    original_text: "北側の入口は瓦礫でふさがれています。",
    text_en: "Debris is blocking the north entrance.",
    media_url: null,
    claimed_location: "Kabukicho Residences, north entrance",
    claimed_geom: null,
    location_precision: "vague",
    received_at: "2026-10-10T09:58:00.000Z",
    sim_time_s: 60,
  },
];

export const mockIncidents: Incident[] = [
  {
    id: MOCK_INCIDENT_ID,
    run_id: MOCK_RUN_ID,
    geom: { type: "Point", coordinates: [139.69185, 35.6896] },
    uncertainty_m: 25,
    status: "corroborated",
    building_id: "bldg_shinjuku_001",
    person_groups: [
      {
        group_id: "group_shinjuku_001",
        est_count: 3,
        condition: "possibly_trapped",
        needs: ["search_and_rescue", "medical_assessment"],
        confidence: 0.76,
      },
    ],
    evidence: {
      supports: [
        "33333333-3333-4333-8333-333333331001",
        "33333333-3333-4333-8333-333333331003",
      ],
      contradicts: [],
    },
    created_at: "2026-10-10T10:03:10.000Z",
    updated_at: "2026-10-10T10:04:20.000Z",
  },
  {
    id: "22222222-2222-4222-8222-222222222038",
    run_id: MOCK_RUN_ID,
    geom: { type: "Point", coordinates: [139.69315, 35.6901] },
    uncertainty_m: 8,
    status: "reported",
    building_id: "bldg_shinjuku_002",
    person_groups: [
      {
        group_id: "group_shinjuku_002",
        est_count: 6,
        condition: "unknown_in_building",
        needs: ["welfare_check", "structural_assessment"],
        confidence: 0.62,
      },
    ],
    evidence: {
      supports: [
        "33333333-3333-4333-8333-333333331002",
        "33333333-3333-4333-8333-333333331004",
      ],
      contradicts: [],
    },
    created_at: "2026-10-10T10:02:12.000Z",
    updated_at: "2026-10-10T10:03:40.000Z",
  },
];

export const mockRoadRestrictions: RoadRestriction[] = [
  {
    run_id: MOCK_RUN_ID,
    road_id: "road_1001_1002",
    status: "LIMITED",
    source_observation_id: "33333333-3333-4333-8333-333333331003",
    updated_at: "2026-10-10T10:04:00.000Z",
  },
  {
    run_id: MOCK_RUN_ID,
    road_id: "road_1002_1003",
    status: "BLOCKED",
    source_observation_id: "33333333-3333-4333-8333-333333331004",
    updated_at: "2026-10-10T10:04:30.000Z",
  },
];

export const mockResources: Resource[] = [
  {
    id: "44444444-4444-4444-8444-444444444101",
    run_id: MOCK_RUN_ID,
    type: "drone",
    name: "Drone Unit 1",
    geom: { type: "Point", coordinates: [139.6912, 35.6895] },
    home_facility_id: "facility_shinjuku_fire",
    status: "en_route",
    capacity: 1,
    skills: ["aerial_search", "building_scan"],
  },
  {
    id: "44444444-4444-4444-8444-444444444102",
    run_id: MOCK_RUN_ID,
    type: "drone",
    name: "Drone Unit 2",
    geom: { type: "Point", coordinates: [139.692, 35.6896] },
    home_facility_id: "facility_shinjuku_fire",
    status: "available",
    capacity: 1,
    skills: ["aerial_search", "thermal_scan"],
  },
  {
    id: "44444444-4444-4444-8444-444444444001",
    run_id: MOCK_RUN_ID,
    type: "rescue_team",
    name: "Rescue Team Alpha",
    geom: { type: "Point", coordinates: [139.6909, 35.6893] },
    home_facility_id: "facility_shinjuku_fire",
    status: "en_route",
    capacity: 6,
    skills: ["urban_search_and_rescue", "medical_first_response"],
  },
  {
    id: "44444444-4444-4444-8444-444444444002",
    run_id: MOCK_RUN_ID,
    type: "rescue_team",
    name: "Rescue Team Bravo",
    geom: { type: "Point", coordinates: [139.692, 35.6896] },
    home_facility_id: "facility_shinjuku_fire",
    status: "available",
    capacity: 5,
    skills: ["urban_search_and_rescue"],
  },
  {
    id: "44444444-4444-4444-8444-444444444003",
    run_id: MOCK_RUN_ID,
    type: "rescue_team",
    name: "Rescue Team Charlie",
    geom: { type: "Point", coordinates: [139.6914, 35.6889] },
    home_facility_id: "facility_shinjuku_fire",
    status: "available",
    capacity: 4,
    skills: ["medical_first_response"],
  },
];

export const mockDecisions: Decision[] = [
  {
    id: "55555555-5555-4555-8555-555555555001",
    run_id: MOCK_RUN_ID,
    incident_id: MOCK_INCIDENT_ID,
    version: 1,
    assignments: [
      {
        resource_id: "44444444-4444-4444-8444-444444444001",
        role: "primary_rescue",
      },
      {
        resource_id: "44444444-4444-4444-8444-444444444101",
        role: "aerial_search",
      },
    ],
    route_geojson: {
      type: "LineString",
      coordinates: [
        [139.6909, 35.6893],
        [139.6913, 35.6895],
        [139.69185, 35.6896],
      ],
    },
    fallback_route_geojson: {
      type: "LineString",
      coordinates: [
        [139.6909, 35.6893],
        [139.6917, 35.6899],
        [139.69185, 35.6896],
      ],
    },
    eta_s: 960,
    status: "approved",
    invalidated_reason: null,
    approved_by: "operator-demo",
    approved_at: "2026-10-10T10:04:45.000Z",
  },
  {
    id: "55555555-5555-4555-8555-555555555002",
    run_id: MOCK_RUN_ID,
    incident_id: "22222222-2222-4222-8222-222222222038",
    version: 2,
    assignments: [],
    route_geojson: null,
    fallback_route_geojson: null,
    eta_s: null,
    status: "draft",
    invalidated_reason: null,
    approved_by: null,
    approved_at: null,
  },
];

export const mockAgentRuns: AgentRun[] = [
  {
    id: "66666666-6666-4666-8666-666666666001",
    run_id: MOCK_RUN_ID,
    agent: "observation_triage",
    input_ref: "observation:33333333-3333-4333-8333-333333331001",
    output: { summary: "Social report linked to Shinjuku Ward Office." },
    duration_ms: 420,
    created_at: "2026-10-10T10:03:15.000Z",
  },
  {
    id: "66666666-6666-4666-8666-666666666002",
    run_id: MOCK_RUN_ID,
    agent: "incident_fusion",
    input_ref: "incident:22222222-2222-4222-8222-222222222042",
    output: {
      summary: "Incident corroborated by social and radio observations.",
    },
    duration_ms: 810,
    created_at: "2026-10-10T10:04:20.000Z",
  },
  {
    id: "66666666-6666-4666-8666-666666666003",
    run_id: MOCK_RUN_ID,
    agent: "route_planner",
    input_ref: "decision:55555555-5555-4555-8555-555555555001",
    output: { summary: "Version 1 route approved; fallback route retained." },
    duration_ms: 1240,
    created_at: "2026-10-10T10:04:45.000Z",
  },
];

export const mockNotifications: Notification[] = [
  {
    id: "77777777-7777-4777-8777-777777777001",
    run_id: MOCK_RUN_ID,
    decision_id: "55555555-5555-4555-8555-555555555001",
    decision_version: 1,
    recipient: "Rescue Team Alpha",
    sent_at: "2026-10-10T10:04:50.000Z",
    acked_at: null,
  },
];

export const mockOperationalEvents: OperationalEvent[] = [
  {
    event_id: "88888888-8888-4888-8888-888888888001",
    run_id: MOCK_RUN_ID,
    table_name: "ops.decisions",
    row_id: "55555555-5555-4555-8555-555555555001",
    payload: { version: 1, status: "approved" },
    created_at: "2026-10-10T10:04:45.000Z",
  },
];

// Ground-truth records are surfaced only through the simulator's reveal-truth API.
export const mockScenario: Scenario = {
  id: MOCK_RUN_ID,
  seed: mockSimulationRun.seed,
  hazard_types: mockSimulationRun.hazard_types,
  params: JSON.stringify({ magnitude: 7.3, tsunami: true, region: "Shinjuku" }),
  map_version_id: MOCK_MAP_VERSION_ID,
  simulator_version: "tokyo-sim-1.0.0",
  started_at: mockSimulationRun.started_at,
  ended_at: null,
};

export const mockTruthBuildingDamage: TruthBuildingDamage[] = [
  {
    event_id: "truth-event-building-001",
    scenario_id: MOCK_RUN_ID,
    building_id: "bldg_shinjuku_001",
    intensity: 0.71,
    damage_level: "moderate",
    simulation_time_s: 190,
    sampled_attributes: JSON.stringify({ structure: "rc", era: "post1981" }),
  },
];

export const mockTruthRoadStatus: TruthRoadStatus[] = [
  {
    event_id: "truth-event-road-001",
    scenario_id: MOCK_RUN_ID,
    road_id: "road_1002_1003",
    status: "blocked",
    cause: "debris",
    simulation_time_s: 210,
  },
];

export const mockTruthPeople: TruthPerson[] = [
  {
    event_id: "truth-event-people-001",
    id: "group_shinjuku_001",
    scenario_id: MOCK_RUN_ID,
    building_id: "bldg_shinjuku_001",
    count: 3,
    condition: "uninjured",
    mobility: "mobile",
    language: "ja",
    simulation_time_s: 190,
  },
];

export const mockTruthLinks: TruthLink[] = [
  {
    scenario_id: MOCK_RUN_ID,
    observation_id: "33333333-3333-4333-8333-333333331001",
    truth_entity_type: "people",
    truth_entity_id: "group_shinjuku_001",
    truth_simulation_time_s: 190,
    match_type: "matched",
  },
];

export const mockSimulationEvents: SimulationEvent[] = [
  {
    event_id: "88888888-8888-4888-8888-888888888001",
    run_id: MOCK_RUN_ID,
    table_name: "ops.decisions",
    row_id: "55555555-5555-4555-8555-555555555001",
    payload: { version: 1, status: "approved" },
    created_at: "2026-10-10T10:04:45.000Z",
  },
];
