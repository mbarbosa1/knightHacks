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

## 9. Database summary (full spec: `../database/final-database.md`)
**State on 10 Oct, afternoon (from `main`'s README, not tested from here):** Neon's dev branch is on the agreed design (`../database/schema.sql`) with map version `shinjuku-v1` loaded (5,335 buildings, 1,818 roads, 13 facilities). Camila's FastAPI backend in `../backend/` is the simulator's way in: bulk GeoJSON at `GET /map/versions/{id}/buildings|roads|facilities` (every column in `properties`, road node ids as strings), `POST /runs`, `POST /runs/{id}/observations`, `PUT /runs/{id}/resources/{id}`, `PUT .../road-restrictions/{road_id}`, `POST /runs/{id}/end`. Arwa's Databricks truth tables (`knighthacks.truth.*`) exist in the team workspace; they need `event_id` on `truth_links` rows and `scenarios.params` as a JSON string, which `sim/truth.py` does not do yet.
**The map data is not in the repo** (team convention: `data/` is gitignored; decided with Gerard 10 Oct). `simulation/data/` exists only on machines that already have it. Next step: an API-backed storage that fetches the map from the backend and caches it locally, creates the run through the API and posts observations there.
- Neon `static` (agents read-only): map_versions, buildings, road_segments, facilities (type hospital/fire_station/police/shelter, `access_node`, capacity).
- Neon `ops`: simulation_runs (seed, map_version_id), observations, incidents, road_restrictions (OPEN/LIMITED/BLOCKED; change triggers reroute), resources, decisions (versioned, invalidated_reason), agent_runs, notifications, operational_events (outbox).
- Databricks (agents have no credentials): scenarios, truth_building_damage, truth_road_status, truth_people, truth_links, simulation_events, scorecards.
- Simulator writes: ops.simulation_runs, ops.observations, ops.resources, all Databricks truth tables.
- SQL: `../database/schema.sql` (static + ops, roles `map_import` / `simulator` / `agents`; ops->static ids checked by trigger against the run's map version). Data is loaded with the repo's `../scripts/load_static.py` (map version `shinjuku-v1`).

## 9b. 3D map in the frontend (branch `3DMAP`, started 10 Oct)
- `python -m sim.export --seed 42 [--mw 7.4]` (`sim/export.py`) writes `frontend/public/sim/map/{buildings.geojson,roads.geojson,facilities.json}` and `frontend/public/sim/run.json` (gitignored). Map files mirror the backend's bulk endpoints (top-level `id`, `geometry`, `properties` incl. `id`; road node ids as strings); `run.json` stands in for `GET /runs/{id}/truth`.
- `frontend/src/map/dataSource.ts` is the only place that knows where data comes from: files by default; `VITE_API_BASE` switches the static layers to the backend; `VITE_SIM_BASE` + `VITE_RUN_ID` switch truth to the simulator endpoint (not built); `VITE_API_BASE` + `VITE_RUN_ID` turn on polling of incidents / road-restrictions / resources (field names from the backend README, untested against a live run).
- `frontend/src/map/ShinjukuMap.tsx`: MapLibre, two views. Ground truth works (damage colours via feature-state, blocked/limited roads, hover). Operational view is a placeholder with empty `incidents`, `units`, `routes` layers ready. Wired into `App.tsx` in place of `MapIllustration` (left in the file, unused), plus zoom/locate buttons, the "Road graph" checkbox, legend and credits.
- `sim/server.py` is the simulator as a local FastAPI service (`python -m uvicorn sim.server:app --port 8100`): `POST /runs {seed?, mw?}` creates a run and returns its truth (new random seed when omitted; `mw` only overrides the magnitude), `GET /runs/{id}/truth` returns it again (last 20 runs in memory). No auth, localhost only, never for agents. The map's "New earthquake" card (magnitude slider 5.8-8.0, random-magnitude checkbox, Generate) calls it; controls are disabled when it is not running. Facilities are small lettered pins with the name on hover (Gerard found full labels cluttered).
- Plan agreed with Gerard: keep on `3DMAP`, visible only locally; switch the data source to the API when Camila's side is ready; then PR to main. Truth reaches the browser through the simulator, never through the backend API.

## 9c. Connected setup (on main since 10 Oct, evening)
- Camila's work: `sim/api_storage.py` (`ApiStorage`), `sim/submission.py`, backend `POST /runs/{id}/ingest`, `GET /runs/{id}/ingestion`, `GET /runs/{id}/observations`. `./dev.sh` starts backend + simulator + website in Neon mode; Generate saves reports and 13 responder units to Neon in one transaction and the map follows that run. `SIM_LOCAL_ONLY=1` makes the simulator local-only.
- **A seed gives the same earthquake from every source.** `sim/storage.py` `make_static_map` is the only place a `StaticMap` is built: entries ordered by id, REAL columns rounded (`DECIMALS`), `era_probs` in `ERAS` order, graph from `build_graph`. Verified against live Neon for three earthquakes (damage, roads, people, reports, stand-in units all identical). `road_graph.graphml` is no longer read.
- **Known gaps, noted by Gerard:**
  - No offline fallback: `dev.sh` and the website both require Neon now. If the venue Wi-Fi fails there is no way to show an earthquake from the UI. Plan B is `SIM_LOCAL_ONLY=1` plus a frontend change to display unsaved runs.
  - All reports are written to Neon at Generate, not over time. Until the live loop releases them on a clock, every consumer (agents, dashboard feed) must filter `sim_time_s <= current simulated time` and sort by `sim_time_s`, with one shared clock (seconds since `started_at` x an agreed speed). The feed endpoint orders by `received_at, id`, which is effectively random within one ingest.
- `backend/.env` on Gerard's machine holds only the `simulator` login on purpose (no owner / map_import), so the destructive scripts cannot run from here.

## 9d. Live loop (started 11 Oct; decisions from Gerard)
Decided: reports arrive over time; speeds 10x / 20x (30x dropped 11 Oct: Gerard prefers a smooth demo over a fast one); one run plays at a time; a stand-in dispatcher moves units until the agents exist and must be easy to swap out (the movement code will only consume *decisions*, the same rows agents will write, so the stand-in must work from reports alone, not the answer key); one seeded mid-run collapse per run, with a switch to turn it off.
- **Piece 1, done: clock + timed reports.** `sim/live.py` `LiveRun` owns the clock for one run (simulated s = real s x speed) and releases each report at its `sim_time_s` through `storage.save_observation`; a failed save is retried in order. `sim/server.py`: Generate saves the run + 13 units (snapshot has no reports), then `start_live`; `GET /clock`, `POST /clock {speed, playing}`, `POST /stop`. Starting a run, or Reset, stops the previous one and ends it in Neon (`completed`). `start_run(..., release_reports=False)` is what the service uses; the command line still releases everything at once. Map bar shows T+mm:ss, Pause, 10x/20x/30x, "N of M reports in". Verified live against Neon: reports arrive in time order.
- Consequence for `scripts/check_sim_run.py` (Camila's): it reports "truth_links point at observations not in Neon" for a run stopped early, because the answer key exists for reports not yet released. That is expected now, not a leak.
- Next pieces: 2 units moving (positions via `PUT /runs/{id}/resources/{id}`), 3 blocked-road discovery (`responder_update` + road restriction), 4 the mid-run collapse, 5 people worsening. Needed from others: decisions endpoints (Camila); a way for agents to call routing (plan: tool endpoints on the simulator service that use beliefs only).

## 9e. Agent architecture (as Gerard described it, 11 Oct) — replaces the 4-agent plan in section 11
Use this description only. A diagram of it was also shared, but its extra boxes ("code validates chosen plan", "reporting agent", "save proposal for review") were added by another Claude chat and Gerard does not know what was meant by them: do not design around those.
1. First layer: **input agent** (the orchestrator).
2. In parallel: **detection**, **medical** and **verification** agents.
3. **Assessment coordination.**
4. Sent to the response structure, in parallel: **resource agent** and **route agent**.
5. Result: a reachable team with routes.
6. **Plan agent:** the overall response and the report.
Agreed order of work (11 Oct): finish the live loop first, talk about the agents after. What the simulation side has to offer them, whatever their internals: reports arriving over time (done), `ops.resources` kept current, and routing callable on believed road status.

## 9f. Response side, step 1: units and agent tools (branch `response-side`, 11 Oct)
- Each run played by the simulator service gets a `Fleet` (`sim/units.py`) of the 13 units. The clock (`sim/live.py`) moves them every tick and saves each unit to `ops.resources` when its status changes and about every 2 real seconds while it drives.
- A unit that drives into a truly blocked road stops, posts a `responder_update` observation (`original_text` is JSON: `unit_id, road_id, road_ids, status`), writes `ops.road_restrictions` with that observation as the source, then reroutes (decision version + 1) or, with no way through, goes back to `available`. On arrival it becomes `busy` and works (next point).
- Rescue model (`sim/units.py`, constants at the top). An engine frees one trapped person per 60 simulated seconds at a collapsed building, 40 at severe, 20 otherwise. An ambulance treats one critical person per 30 s. A patrol helps nobody: after 120 s it reports the true damage level and counts (`action: assessed`). Two units of a type at one building each work at that rate, so twice as fast. When nothing is left for its skill the unit sends a structured `responder_update` (`action: rescued | treated | nothing_found`, `count`, `still_trapped`/`still_critical`) and is `available` where it stands. A unit with nothing to do there loses 60 s first. In the truth, people move from the `trapped`/`critical` group to a `{building}:rescued` / `{building}:treated` group (new `truth_people` rows). `GET /clock` carries `rescued` and `treated` totals; `GET /units` carries `helped` per unit.
- People left waiting get worse (`WORSEN_*` constants in `sim/units.py`): from 10 simulated minutes, every 5 minutes a fifth (rounded up) of the trapped at a building become critical and a fifth of the critical die, unless a unit with the right skill is working there. `GET /clock` carries `died_waiting`.
- Map (`frontend/src/map/ShinjukuMap.tsx`): polls `GET /units` twice a second and draws each dispatched unit as a square lettered badge (E/A/P) with a dashed route ahead; a halo means working on scene, faded means free again. Units never sent are not drawn. The bar shows rescued and treated totals. Operational view only.
- Not modelled yet: injured (walking wounded) people, taking patients to hospital, mid-run collapses. The truth view fetched at Generate (`/runs/{id}/truth`) does not change as people are rescued.
- The clock moves units first and saves reports on a second thread. Before this, at 20x the one thread spent all its time saving reports (about 0.3 s each to Neon) and units barely moved. Reports still reach Neon at only about 3 a second, so at 20x a big earthquake's reports arrive late (113 saved of 389 due after 43 real seconds). Decided 11 Oct: play at 10x. `DEFAULT_SPEED` in `sim/live.py` is 10. The website's `DEFAULT_SPEED` in `frontend/src/map/dataSource.ts` is 10 too; 20x is still a button. A bulk endpoint (`POST /runs/{id}/observations/batch`, one multi-row insert) would make 20x keep up; not built.
- Tools for the response agents on the simulator service (port 8100), all planning on believed road status (`ops.road_restrictions`, re-read every 3 s, plus what units found), never the truth:
  `POST /tools/nearest-units {building_id | lon+lat, type?, k?}`, `POST /tools/route {unit_id | from_lon+from_lat, building_id | lon+lat}`, `POST /dispatch {unit_id, building_id | lon+lat, incident_id?}` (201 with the decision, 409 with the reason), `GET /units` for the map. Optional `run_id` on each; 409 if it is not the run being played.
- `SIM_AUTO_DISPATCH=1` sends the nearest free unit to the stand-in's targets, for testing without agents. Off by default.
- Every decision is saved to `ops.decisions` through `PUT /runs/{id}/decisions/{decision_id}` (added to the backend with `GET /runs/{id}/decisions`). A reroute marks the old row `invalidated` with the reason and writes a new row (new id, next version). Versions count up per incident because the table allows one row per incident and version. A decision the backend refuses (an `incident_id` not in the run) is dropped, not retried, so it cannot hold up unit positions.
- Not done: the map does not draw moving units yet; step 2 is adding these tools to `agents/tools.py`, step 3 the resource and plan agents.

## 10. Open TODOs
- Note (10 Oct): this folder was renamed from `sim-data-package/` to `simulation/` when it was merged with `main`. `scripts/plateau/` was removed (see `docs/PROVENANCE.md`); the loader and validator now live at the repo root in `../scripts/`; the duplicate schema and design doc were removed in favour of `../database/`. `map_id` is NOT unique, never key on it.
- [x] J-SHIS surface-soil mesh loaded (10 Oct): `data/jshis/` CSV -> `soil_amp` on all 5,335 buildings, ARV 1.29-1.64. Loaded into Neon by Camila.
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
| Final DB design | `../database/final-database.md` | `database/final-database.md` |
| Simulator runtime | `docs/simulator-runtime.md` | `simulation/simulator-runtime.md` |
| Data coverage | `docs/COVERAGE.md` | `data/plateau/COVERAGE.md` |
| Damage formulas | `docs/REFERENCE.md` | `data/plateau/REFERENCE.md` |
| Damage code | `sim/damage_model.py` | `data/plateau/damage_model.py` |
| Buildings (DB-shaped, identical copy) | `data/buildings.geojson` | `data/plateau/buildings_table.geojson` |
| Buildings (full, with per-field sources) + stats | `data/plateau_full/` | `data/plateau/` |
| Facilities (13) | `data/facilities.geojson` | `data/facilities/facilities.geojson` |
| Bounding boxes | `data/bbox.json` | — |
| Road graph outputs (after running the script) | `data/road_graph.graphml`, `data/road_segments.geojson`, `data/road_segments.meta.json` | — |
| SQL schema | `../database/schema.sql` | — |
| Static loader / validator | `../scripts/load_static.py`, `../scripts/validate.py` | — |
| Road graph builder / J-SHIS soil loader | `scripts/build_road_graph.py`, `scripts/load_jshis_soil.py` | — |
| Demo prototype (approx. roads) + demo attributes | `demo/` | `demo/`, `data/plateau/demo_building_attributes.json` |
