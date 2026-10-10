# CLAUDE.md — Simulation branch handoff (Gerard)

## 1. Project in 5 lines
- Multi-agent disaster-response platform for Shinjuku, Tokyo (~1.5 km box around Shinjuku Station, centre 35.6938 N, 139.7034 E).
- Simulator creates a random disaster (new seed each run, replayable from the seed); noisy reports stream in; AI agents turn them into incidents, assignments, routes and briefings; a human approves; "reveal truth" scores the agents.
- Knight Hacks, 36 hours, team of 4. Goal by hour 20: one earthquake scenario end to end in the UI.
- Judges are American: all UI, briefings, agent output and place names in English (Japanese kept in `*_ja` fields).
- Stack: React + MapLibre GL (3D buildings), FastAPI/Python, OSMnx + NetworkX, Neon (Postgres + PostGIS), Databricks, S3/SNS, Gemini, Hugging Face (Whisper).

## 2. Gerard's role
Simulation: damage physics, scenario + report generator, road graph + routing (agent tool functions).
Nothing is coded yet; this branch starts it. Teammates: backend/data (Neon, FastAPI, Databricks export), agents (Gemini, Whisper), frontend (React/MapLibre).

## 3. Decisions (locked) and scrapped
Locked:
- Hazards: earthquake first, then flood. Compound disasters = stretch.
- Hidden truth vs noisy observations vs agent beliefs (below). Agents never read truth.
- IDs shared across map, NetworkX and DB: buildings `id` (`bldg_<uuid>`), roads `u_v_key`.
- Unknown stays NULL; never guess defaults. SRID 4326 everywhere.
- Gemini only writes wording; code decides facts.
Scrapped: Unity (3D done in MapLibre), VLM/drone imagery, fire spread (cut for now; may return — formulas still in docs/REFERENCE.md §6).
If behind, cut: extra channels -> flood -> audio -> fancy UI. Never cut: truth vs reports, live map, reroute moment, human approval, reveal-truth scorecard.

## 4. Core design
1. **Hidden truth** (simulator memory + Databricks): building damage, road status, people (count, condition, mobility, language).
2. **Observations** (Neon `ops.observations`): reports generated from truth with noise — duplicates, wrong counts, vague locations, contradictions, rumors with no truth, missed events. Each has a hidden answer-key row in Databricks `truth_links` (match_type matched / misleading / fabricated / unmatched).
3. **Agent beliefs**: `ops.incidents`, `ops.road_restrictions`, `ops.decisions`.
**Reveal truth** compares 3 vs 1: trapped people found, duplicates merged, location error, time to dispatch, routes through actually-blocked roads.
Key demo moment: new event (road collapse) -> plan v1 invalidated -> reroute v2 -> briefing explains why.

## 5. Damage model
Chain (all in `docs/REFERENCE.md`, implemented in `sim/damage_model.py`; units km, cm/s):
1. Si & Midorikawa (1999) PGV on bedrock (Vs600) from Mw, depth, distance; optional scatter sigma ~0.23 log10 (sigma value marked inferred).
2. Soil amp: `PGV_surface = PGV600 * 1.31 * ARV_jshis` (J-SHIS 250 m mesh). Fallback from AVS30.
3. JMA intensity `I = 2.68 + 1.72*log10(PGV)`, shown as e.g. "Intensity 6+ (Japan scale)".
4. Murao & Yamazaki (2000) lognormal fragility in PGV by structure x era -> P(collapse), P(collapse+half); dice roll. **Mismatch:** `damage_model.sample_damage` returns `collapse / half / none`, but truth tables use `none / light / moderate / severe / collapsed`; the simulator must map them (ours to define, UNCERTAIN).
5. Validate: Monte Carlo vs TMG 2022 Shinjuku-ku estimate (scale by building count; box ~1/8 of ward). Kobe curves run high for modern buildings: shift lambda up / tune era priors, don't change shaking.
Collapses next to roads can block them (see §7).

Data reality (5,335 PLATEAU buildings):
- Real: footprint 100%, height 96%, floors 90%, usage 90%, fire-resistance 90% (gives wood vs non-wood).
- Inferred: RC vs steel (from height/floors; `structure_basis` says which rule), era (no year built exists for Tokyo). Totals wood 1,973 / rc 3,247 / steel 71 / light_steel 44.
- Era: each building has `era_probs`; **re-roll era every run** (`damage_model.sample_damage` does it). Priors are the calibration knob.
- Soil: `soil_amp` is NULL until J-SHIS mesh is loaded. `damage_model.pgv_surface` falls back to the Vs400 baseline (ARV = 1); or use a placeholder ARV (e.g. ~1.4, UNCERTAIN) and flag it.
- Every inferred field is marked via `*_source` = inferred.
- **`year_built` is filled for every building but is a seeded guess inside the drawn era** (`year_source = inferred`). Don't use it; use `era` / `era_probs`.

## 6. Report channels
MVP 4: `emergency_call_119` (audio via TTS), `eyewitness_text`, `social_media`, `fire_police_radio`.
Later: `hospital_feed`, `shelter_update`, `ward_bulletin`, `sensor`, `responder_update` (the tick loop already emits responder_update when a unit hits a blocked road).
Each channel has its own delay, accuracy, location precision (gps / address / landmark / vague), format, language and `source_reliability` (0-1). Mix ~60% English / 40% Japanese & other.
Code decides facts, delays and errors; **Gemini only writes the human wording** (plus `text_en` translation).

## 7. Road graph and routing plan
Graph:
- OSMnx `drive` network over `road_bbox` in `data/bbox.json` (buildings extent + 150 m). Script exists: `scripts/build_road_graph.py` -> `data/road_graph.graphml`, `data/road_segments.geojson`, `data/road_segments.meta.json`, fills `access_node` in `data/facilities.geojson`. Then `scripts/validate.py` and `scripts/load_static.py --replace`. Must run locally (sandbox could not reach OSM).
- Edge id = `f"{u}_{v}_{key}"`, identical in NetworkX, `static.road_segments.id`, map layer.
- Facility `access_node` = nearest node in the graph's largest strongly connected component (haversine, done by the script). Buildings have no `access_node` column; compute at load time if needed.
Road damage (truth):
- Debris: buffer each collapsed/severe building by ~half its height (debris reach; heuristic, UNCERTAIN); query an STRtree of edge geometries (projected metres); intersecting edges -> BLOCKED (or LIMITED if partial overlap; threshold TBD).
- Flood depth: > 30 cm BLOCKED, 10-30 cm LIMITED, < 10 cm OPEN.
- Two dicts: `truth_status[edge_id]` (simulator) and `belief_status[edge_id]` (from `ops.road_restrictions`, written by agents). Planning uses belief; unit movement checks truth.
Weight function (passed to NetworkX):
```python
def weight(u, v, d, status):  # MultiDiGraph: d is {key: attrs}; take min over keys
    s = status.get(f"{u}_{v}_{k}", "OPEN")
    OPEN -> d["travel_time"]; LIMITED -> d["travel_time"] * 3; BLOCKED -> None (edge skipped)
```
Algorithms:
- Route: A* (haversine/speed heuristic) on belief weights; alternative route = re-run with used edges penalised (e.g. x5) -> `fallback_route_geojson`.
- Nearest unit: multi-source Dijkstra from all available units' nodes (or reverse Dijkstra from incident).
- Facility choice: hospitals/shelters ranked by travel time + capacity (NULL capacity = unknown, not zero).
- Assignment: cost matrix units x incidents (travel time, infeasible = large), `scipy.optimize.linear_sum_assignment`.
- Reachability: `nx.has_path` on graph filtered to non-BLOCKED edges.
- Replanning: on `road_restrictions.updated_at` change, check each active decision's route edges; if any now BLOCKED/LIMITED, mark decision invalidated (reason) and compute v2.
- Unit movement: per tick (1 s sim time) advance along route by speed; on entering an edge check truth; if BLOCKED stop and emit `responder_update`.
Agent tool functions (plain Python, called by agents):
`route(from, to)`, `nearest_units(incident, type, k)`, `facility_options(location, type)`, `is_reachable(a, b)`, `assign(units, incidents)`.

## 8. Runtime
- Truth lives in memory (`TruthState`: building_damage, road_status, people, sampled_attributes, truth_links).
- Every change appends a row with unique `event_id` to `pending_truth_writes`; flusher writes to Databricks every ~10 s and at run end (dedupe by event_id).
- Observations go to Neon `ops.observations` immediately on release.
- `GET /runs/{id}/truth` serves TruthState from memory (operator reveal-truth only).
- Start of run: load static map -> create simulation_runs + scenarios rows -> sample eras, compute damage/roads/people -> schedule observations -> tick loop.
- Storage interface: `save_truth_batch(rows)`, `save_observation(obs)`, `load_static_map(version)`. **Implement local-file (JSON/GeoJSON) version first**; swap to Neon/Databricks without touching sim logic.

## 9. Database summary (full spec: `docs/final-database.md`)
**Reality check (10 Oct, from Camila's `origin/database-setup` branch, commit 6a780f8):** two database designs exist.
- **Old design, live on Neon main and dev** (`database/001`, `002` on her branch, marked "RECORD - DON'T RUN"): schema `"staticMapData"` (must be double-quoted), text ids, different ops column names (`language`, `location`, `simulation_time_s`, `road_graph_version` NOT NULL). Versus our data files it lacks buildings `height_source, floors_source, basement_floors, footprint_m2, usage_source, era, zoning, zoning_ja, facility_subtype, facility_name_en, plateau_building_id` and facilities `access_node, subtype, building_match, source, note`. It does have `era_probs`, `soil_amp`, `structure` and all road columns.
- **Agreed design, not yet applied** (`database/schema.sql` on her branch, identical to our `sql/schema.sql`): schema `static`. Her FastAPI backend (`backend/app/repository.py`) already queries `static.*` with the agreed columns. Her `scripts/apply_schema.py` drops the old design and applies it; it had not been run when she reported the mismatch.
- Her branch has no map data (`data/` is gitignored; README says Neon had zero map rows).
Confirm which design is live before writing the Neon version of `sim/storage.py`. Observations should go through her API (`POST /runs`, `POST /runs/{id}/observations`), not straight into Neon.
- Neon `static` (agents read-only): map_versions, buildings, road_segments, facilities (type hospital/fire_station/police/shelter, `access_node`, capacity).
- Neon `ops`: simulation_runs (seed, map_version_id), observations, incidents, road_restrictions (OPEN/LIMITED/BLOCKED; change triggers reroute), resources, decisions (versioned, invalidated_reason), agent_runs, notifications, operational_events (outbox).
- Databricks (agents have no credentials): scenarios, truth_building_damage, truth_road_status, truth_people, truth_links, simulation_events, scorecards.
- Simulator writes: ops.simulation_runs, ops.observations, ops.resources, all Databricks truth tables.
- SQL: `sql/schema.sql` matches final-database.md (static + ops, roles `map_import` / `simulator` / `agents`; ops->static ids checked by trigger against the run's map version). Tested on local PostGIS 16. Load data with `scripts/load_static.py` (map version `shinjuku-v1`).

## 10. Open TODOs
- Note (10 Oct): `scripts/plateau/`, `scripts/load_static.py` and `scripts/validate.py` were removed from the repo; `docs/PROVENANCE.md` records what they did (inference rules, era priors, validator checks). `map_id` is NOT unique, never key on it.
- The DB teammate loads Neon by hand and does not want generated loader scripts; ignore references to the removed scripts in this file and in `README.md`.
- [x] J-SHIS surface-soil mesh loaded (10 Oct): `data/jshis/` CSV -> `soil_amp` on all 5,335 buildings, ARV 1.29-1.64. DB teammate still has to run `load_static.py --replace`.
- [x] `scripts/build_road_graph.py` run (10 Oct): 844 nodes, 1,818 edges; graph ids == `road_segments.geojson` ids. 54 nodes are outside the largest strongly connected component.
- [x] Facility `access_node` filled and verified (all 13 in the graph core). Facility `capacity` is NULL for all 13.
- [ ] Tune fragility/era priors vs TMG 2022 Shinjuku estimate (Monte Carlo).
- [ ] Replace demo's approximate roads with OSMnx `road_segments`.
- [x] Five-level mapping agreed with Gerard and implemented in `sim/damage.py`: collapse -> collapsed; half -> severe 40 % / moderate 60 %; light = collapse+half curve shifted by -0.7 in lambda. Split values are ours (UNCERTAIN, tunable).
- [ ] Debris-reach and LIMITED thresholds are heuristics; tune visually. First cut in `sim/roads.py`: collapsed blocks within height/2, severe limits within height/4, reach clamped to 2-20 m.
- [ ] Scenario ranges in `sim/scenario.py` are ours: Mw 5.8-8.0 (Gerard's decision: smaller quakes damage nothing), depth 10-30 km, epicentre 5-30 km. `--mw` fixes the magnitude (future UI slider).
- [ ] Simulator outputs must feed the deployed MapLibre frontend as small payloads keyed by the shared ids; label facilities on the map.

## 11. Suggested first coding steps
Done so far (10 Oct): steps 1-5, in `sim/` (`scenario.py`, `truth.py`, `storage.py`, `damage.py`, `roads.py`, `routing.py`, `run.py`) with `tests/`. Run `python -m sim.run --seed 42` and `python -m pytest tests` from this folder. Routing is `sim/routing.py` `Router(graph)`: `route` (A*, with `fallback`), `is_reachable`, `affected_edges`, `nearest_units`, `facility_options`, `assign`; all take a plain `{road_id: status}` map (agents' beliefs to plan, truth to move). People are `sim/people.py` (step 6 done): occupancy at a fixed weekday 12:00 (Gerard's decision for the demo) from use x floor area, then conditions by damage level; every number there is our assumption. Responder units are simulated vehicles/teams (rows in `ops.resources`), NOT AI agents: the agents are the dispatch software that assigns them. Reports are `sim/reports.py` (step 7 done): `generate_reports(static_map, sc, truth)` -> observations (ops.observations columns) + truth_links, deterministic per seed, templates in en/ja (ja not checked by a native speaker; zh/ko not written), format always `text` (no audio yet). `sim/lookup.py` `MapIndex` turns a point + street name into road ids and a point into a building (the agents' code step). `run.py` releases all reports at once until the live loop exists. Agent design agreed 10 Oct: 4 AI agents (detect, medical, verify, plan); resource + route are code; structured `responder_update` reports carry JSON `{road_id, status}` in `original_text` and are written to road_restrictions by code. Next: step 8 (live loop: units, movement, responder_update, scheduled road collapse, report release).
1. `pip install -r requirements.txt`; run `scripts/build_road_graph.py`; load graph with `osmnx.load_graphml`; sanity-check ids match `road_segments.geojson`.
2. Storage interface + local-file implementation; `load_static_map` reading buildings/facilities/roads.
3. `TruthState` + seeded scenario params (epicentre, Mw, depth); wrap `sim/damage_model.py` to fill building_damage (era re-rolled, placeholder ARV).
4. Debris -> road truth status via STRtree.
5. Routing module: weight fn, route + alternative, nearest_units, facility_options, is_reachable, assign. Unit tests on toy graph.
6. People generator (per building by usage/floors/time of day) -> truth_people.
7. Report generator: MVP 4 channels, noise + truth_links; Gemini wording stubbed with templates first.
8. Tick loop (units, worsening, scheduled road-collapse event, observation release) + flusher + `GET /runs/{id}/truth`.
9. Monte Carlo calibration script vs TMG estimate.
10. Flood hazard (depth -> road status).

## 12. File map
Paths are relative to this package (copy it into the repo as-is). Originals live in the project files; the PLATEAU thread owns them.
| What | In package | Original |
|---|---|---|
| Package README (setup, missing pieces) | `README.md` | — |
| Python deps | `requirements.txt` | — |
| Project context (partly stale) | `docs/project-context.md` | `project-context.md` |
| Final DB design | `docs/final-database.md` | `database/final-database.md` |
| Simulator runtime | `docs/simulator-runtime.md` | `simulation/simulator-runtime.md` |
| Data coverage | `docs/COVERAGE.md` | `data/plateau/COVERAGE.md` |
| Damage formulas | `docs/REFERENCE.md` | `data/plateau/REFERENCE.md` |
| Damage code | `sim/damage_model.py` | `data/plateau/damage_model.py` |
| Buildings (DB-shaped, identical copy) | `data/buildings.geojson` | `data/plateau/buildings_table.geojson` |
| Buildings (full, with per-field sources) + stats | `data/plateau_full/` | `data/plateau/` |
| Facilities (13) | `data/facilities.geojson` | `data/facilities/facilities.geojson` |
| Bounding boxes | `data/bbox.json` | — |
| Road graph outputs (after running the script) | `data/road_graph.graphml`, `data/road_segments.geojson`, `data/road_segments.meta.json` | — |
| SQL schema | `sql/schema.sql` | — |
| Static loader / validator | `scripts/load_static.py`, `scripts/validate.py` | — |
| Road graph builder / J-SHIS soil loader | `scripts/build_road_graph.py`, `scripts/load_jshis_soil.py` | — |
| Demo prototype (approx. roads) + demo attributes | `demo/` | `demo/`, `data/plateau/demo_building_attributes.json` |
