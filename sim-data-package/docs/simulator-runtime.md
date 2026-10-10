# Simulator runtime: how truth is held and stored

## Rule
During a run, the simulator keeps ground truth in memory. Databricks is the permanent record, written in batches. Neon receives observations immediately. Agents never see truth.

## Components (simulator code)
- `TruthState` (in memory, per run): `building_damage[building_id] -> {intensity, damage_level, sim_time_s}`, `road_status[road_id] -> {status, cause, sim_time_s}`, `people[group_id] -> {building_id, count, condition, mobility, language, sim_time_s}`, `sampled_attributes[building_id] -> {era, structure, ...}`, `truth_links` list.
- Tick loop (e.g. 1 s sim time): advance units along routes and check `road_status` in TruthState when a unit enters a road (blocked -> stop unit, emit responder_update observation); worsen waiting people over time; apply scheduled events (aftershocks, road changes); release due observations.
- `pending_truth_writes`: every TruthState change appends a row with a unique `event_id`.
- Flusher: every ~10 s and at run end, writes `pending_truth_writes` to Databricks truth tables (truth_building_damage, truth_road_status, truth_people, truth_links, scenarios), then clears the list. Databricks dedupes on `event_id` so retries don't double count.
- Observations: written to Neon `observations` the moment they are released (agents react live).
- Truth endpoint: `GET /runs/{run_id}/truth` serves TruthState from memory; used only by the operator's reveal-truth view. Agents have no access to this endpoint or to Databricks credentials.
- After the run: scoring/comparison reads Databricks.

## Start-of-run sequence
1. Load map version from Neon `static` schema (buildings, road_segments, facilities) + road graph.
2. Create `simulation_runs` row in Neon and `scenarios` row (batched) for Databricks with seed, hazard, params, map_version_id, simulator_version.
3. Sample per-run attributes (era etc.), compute damage, roads, people into TruthState.
4. Schedule observations on the timeline; start tick loop.

## Development without databases
Hide storage behind a small interface: `save_truth_batch(rows)`, `save_observation(obs)`, `load_static_map(version)`. First implementation writes/reads local JSON/GeoJSON files; swap to Neon/Databricks later without touching simulation logic.

## Related decision
Static map lives in the same Neon project as a separate `static` schema (agents read-only); operational tables in an `ops` schema. Truth is the only data in a separate system (Databricks).
