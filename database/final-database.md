# Final database design (v1)

> This is the agreed version of the database design.

Two systems: **Neon Postgres + PostGIS** (schemas `static`, `ops`) and **Databricks** (ground truth + scoring).

## System 1: Neon Postgres + PostGIS

### Schema `static` (agents: read-only)

#### map_versions
| column | type | notes |
|---|---|---|
| id | text PK | |
| created_at | timestamptz | |
| source_versions | jsonb | PLATEAU / OSM / J-SHIS versions |
| notes | text | |

#### buildings
PK (map_version_id, id). Columns match `data/plateau/buildings_table.geojson` properties.
| column | type | notes |
|---|---|---|
| map_version_id | text FK map_versions | |
| id | text | e.g. `bldg_<uuid>` |
| geom | geometry(Polygon,4326) | GiST |
| height_m | real | |
| height_source | text | PLATEAU / inferred |
| floors | int | |
| floors_source | text | |
| basement_floors | int | |
| footprint_m2 | real | |
| structure | text | wood / rc / steel / light_steel |
| structure_source | text | |
| structure_basis | text | reasoning for inferred structure |
| fire_resistance | text | e.g. fire-resistant |
| fireproof_code | text | PLATEAU code |
| year_built | int | |
| year_source | text | |
| era | text | pre1971 / 1971_1981 / post1981 |
| era_probs | jsonb | probability per era |
| usage | text | |
| usage_code | text | PLATEAU code |
| usage_source | text | |
| zoning | text | |
| zoning_ja | text | |
| soil_amp | real | NULL until J-SHIS |
| name_en / name_ja | text | |
| address_en / address_ja | text | |
| facility_id | text | FK facilities.id, nullable |
| facility_type | text | nullable |
| facility_subtype | text | nullable |
| facility_name_en | text | nullable |
| map_id | text | id used by the map layer |
| plateau_building_id | text | original PLATEAU id |

#### road_segments
| column | type | notes |
|---|---|---|
| map_version_id | text FK | PK (map_version_id, id) |
| id | text | OSMnx `u_v_key`, same as NetworkX |
| geom | geometry(LineString,4326) | GiST |
| from_node / to_node | bigint | OSM node ids |
| length_m | real | |
| speed_kmh | real | |
| oneway | boolean | |
| name_en / name_ja | text | |

#### facilities
| column | type | notes |
|---|---|---|
| map_version_id | text FK | PK (map_version_id, id) |
| id | text | |
| type | text | hospital / fire_station / police / shelter |
| name_en / name_ja | text | |
| geom | geometry(Point,4326) | GiST |
| building_id | text | FK buildings.id |
| capacity | int | NULL = unknown |
| access_node | bigint | nearest road graph node |
| subtype | text | hospital / branch / police_box / school / open_space |
| building_match | text | inside / nearest (point within 25 m of the building) |
| source | text | e.g. Overture Maps places 2026-09-23.1 |
| note | text | caveats, e.g. assumed shelter |

### Schema `ops` (agents: read/write)

#### simulation_runs
| column | type | notes |
|---|---|---|
| id | uuid PK | = Databricks scenarios.id |
| map_version_id | text FK | only place map version is stored |
| seed | bigint | |
| hazard_types | text[] | |
| status | text | |
| started_at / ended_at | timestamptz | |

#### observations
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| run_id | uuid FK | |
| channel | enum | emergency_call_119, eyewitness_text, social_media, fire_police_radio, hospital_feed, shelter_update, ward_bulletin, sensor, responder_update |
| source_reliability | real | 0-1 |
| format | text | audio / text / structured |
| lang | text | |
| original_text | text | |
| text_en | text | |
| media_url | text | S3 URL |
| claimed_location | text | as stated |
| claimed_geom | geometry(Point,4326) | nullable |
| location_precision | text | gps / address / landmark / vague |
| received_at | timestamptz | |
| sim_time_s | real | |

#### incidents
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| run_id | uuid FK | |
| geom | geometry(Point,4326) | |
| uncertainty_m | real | |
| status | text | reported / corroborated / disputed / resolved |
| building_id | text | nullable; FK (run's map_version_id, building_id) -> static.buildings |
| person_groups | jsonb | [{group_id (stable), est_count, condition, needs, confidence}] |
| evidence | jsonb | {supports: [obs ids], contradicts: [obs ids]} |
| created_at / updated_at | timestamptz | |

#### road_restrictions
| column | type | notes |
|---|---|---|
| run_id | uuid FK | PK (run_id, road_id) |
| road_id | text | FK (run's map_version_id, road_id) -> static.road_segments |
| status | text | OPEN / LIMITED / BLOCKED |
| source_observation_id | uuid | |
| updated_at | timestamptz | change triggers reroute |

#### resources
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| run_id | uuid FK | |
| type | text | |
| name | text | |
| geom | geometry(Point,4326) | |
| home_facility_id | text | FK (run's map_version_id, home_facility_id) -> static.facilities |
| status | text | available / en_route / busy / out_of_service |
| capacity | int | |
| skills | text[] | |

#### decisions
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| run_id | uuid FK | |
| incident_id | uuid FK | |
| version | int | |
| assignments | jsonb | |
| route_geojson | jsonb | |
| fallback_route_geojson | jsonb | |
| eta_s | real | |
| status | text | draft / approved / invalidated / sent |
| invalidated_reason | text | |
| approved_by | text | |
| approved_at | timestamptz | |

#### agent_runs
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| run_id | uuid FK | |
| agent | text | |
| input_ref | text | |
| output | jsonb | |
| duration_ms | int | |
| created_at | timestamptz | |

#### notifications
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| run_id | uuid FK | |
| decision_id | uuid FK | |
| decision_version | int | |
| recipient | text | |
| sent_at / acked_at | timestamptz | |

#### operational_events (outbox)
| column | type | notes |
|---|---|---|
| event_id | uuid UNIQUE | |
| run_id | uuid | |
| table_name | text | |
| row_id | text | |
| payload | jsonb | |
| created_at | timestamptz | exported to Databricks after run |

## System 2: Databricks (agents: no credentials)

#### scenarios
| column | type | notes |
|---|---|---|
| id | string | = ops.simulation_runs.id |
| seed | bigint | |
| hazard_types | array<string> | |
| params | string (json) | |
| map_version_id | string | |
| simulator_version | string | |
| started_at / ended_at | timestamp | |

#### truth_building_damage
| column | type | notes |
|---|---|---|
| event_id | string | dedupe key |
| scenario_id | string | |
| building_id | string | |
| intensity | double | |
| damage_level | string | none / light / moderate / severe / collapsed |
| simulation_time_s | double | |
| sampled_attributes | string (json) | |

#### truth_road_status
| column | type | notes |
|---|---|---|
| event_id | string | |
| scenario_id | string | |
| road_id | string | |
| status | string | |
| cause | string | |
| simulation_time_s | double | |

#### truth_people (new row per change)
| column | type | notes |
|---|---|---|
| event_id | string | |
| id | string | stable person-group id |
| scenario_id | string | |
| building_id | string | |
| count | int | |
| condition | string | |
| mobility | string | |
| language | string | |
| simulation_time_s | double | |

#### truth_links
| column | type | notes |
|---|---|---|
| scenario_id | string | |
| observation_id | string | ops.observations.id |
| truth_entity_type | string | building / road / people |
| truth_entity_id | string | |
| truth_simulation_time_s | double | |
| match_type | string | matched / misleading / fabricated / unmatched |

#### simulation_events (completed-run history)
Exported from `ops.operational_events` after the run; same `event_id` (dedupe key), plus run_id, table_name, row_id, payload, created_at.

Plus: run scorecards.

## Who writes / who reads
| table(s) | writes | reads |
|---|---|---|
| static.* | data pipeline | simulator, agents, UI |
| ops.simulation_runs | simulator | all |
| ops.observations | simulator | agents, UI |
| ops.incidents, decisions, agent_runs, notifications | agents (+ operator approval) | agents, UI |
| ops.road_restrictions | agents / simulator via observations | router, agents, UI |
| ops.resources | simulator, agents | agents, UI |
| ops.operational_events | all ops writers | export job |
| Databricks truth_* / scenarios | simulator | scoring, reveal-truth API |
| Databricks scorecards | scoring job | team / UI |

## Access
- Agents' role: SELECT on `static`; SELECT/INSERT/UPDATE only on the `ops` tables it needs. Owns no tables; cannot CREATE/ALTER/DROP.
- `map_import` role: separate role that maintains `static`.
- Foreign keys cross schemas inside Neon; references into `static` include the map version (the run's pinned `map_version_id`).
- Simulator and evaluation/export services have their own Databricks credentials; agents get none.

## How they connect
- Simulator loads a map version from `static`.
- During a run, truth is held in simulator memory and served for reveal-truth via `GET /runs/{id}/truth`; it is flushed to Databricks every ~10 s and at run end (crash safety); dedupe by `event_id`.
- Observations go to Neon `ops.observations` immediately.
- Agents read/write `ops`, read `static`; no Databricks access.
- Changes to `road_restrictions` trigger reroute (decisions may be invalidated).
- Reveal-truth is served by simulator API `GET /runs/{id}/truth`.
- After the run, `ops` (via outbox) is exported to Databricks for scoring.

## Rules
- IDs are shared across map, NetworkX graph and DB (buildings `id`, roads `u_v_key`).
- Cross-system IDs are validated on ingest.
- Unknown stays NULL (never guessed defaults).
- PostGIS SRID 4326 everywhere; GiST indexes on all geometry columns.
- Media stored in S3; DB holds URLs only.

## Changes vs team draft
- `static` is a schema in the same Neon DB, not a separate server.
- Added building columns fire_resistance, fireproof_code, era, era_probs, facility_type, facility_id, map_id (plus the other columns present in buildings_table.geojson).
- map_version stored only on `simulation_runs`.
- Stable `group_id` per person group in incidents.
- Observation channel values listed explicitly.
- `event_id` on all truth rows.
- Truth held in memory during runs.
