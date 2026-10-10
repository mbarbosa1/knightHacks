# Disaster-Response Platform — Project Context (Knight Hacks, 36h, team of 4)

## What we're building
A multi-agent disaster-response platform for Shinjuku, Tokyo. A simulator creates a random disaster; noisy reports stream in from many channels; AI agents turn them into incidents, needs, resource assignments, routes and English responder briefings; a human coordinator approves; a "reveal truth" view scores how well the agents did. Demo audience: American judges, so everything user-facing is in English.

## Decisions made
- **Location:** Shinjuku ward, ~1.5–2 km box around Shinjuku Station / Kabukichō.
- **Hazards:** earthquake (first, fully done) then flood. Fire spread is cut for now (may return). Compound disasters are a stretch goal.
- **Every run is different:** a new random seed each run, but replayable from the seed.
- **Stack:** React + MapLibre GL (3D buildings), FastAPI/Python, OSMnx + NetworkX routing, Neon (Postgres + PostGIS) for live state, Databricks for analysis, AWS S3 (audio/images) + SNS (test notification), Gemini (report writing + agent reasoning), Hugging Face (e.g. Whisper for audio).
- **No Unity.** 3D digital-twin look done in the browser with MapLibre extruded buildings.
- **VLM / drone imagery: scrapped.**
- **English everywhere** judges look: UI, briefings, agent output, romanized place names. Original Japanese kept in separate fields; reports show English translation first with an "original" toggle. Report mix ~60% English / 40% Japanese & other.

## The core idea: hidden truth vs noisy reports
1. **Hidden truth** (simulator only): what actually happened — building damage, blocked roads, people and their condition.
2. **Reports** (what agents see): messages generated *from* the truth with realistic noise — duplicates, wrong counts, vague locations, contradictions, rumors with no truth behind them, missed events. Each report has a hidden answer-key link to its truth event, used only for scoring.
3. **Agent beliefs & decisions:** incidents, situations, road beliefs, plans, notifications.
Agents can never read the truth tables (separate DB role). **Reveal truth** compares 3 vs 1: trapped people found, duplicates merged, location error, time to dispatch, routes through actually-blocked roads.

## How the simulation decides damage (the "professional chain")
1. Ground-motion equation (Si & Midorikawa): magnitude, depth, distance → shaking at each building.
2. Soil amplification (J-SHIS 250 m grid) — soft ground shakes harder. *Needs a teammate to download the J-SHIS mesh for Shinjuku (5339) on a laptop; blocked from our sandbox.*
3. Convert to Japan's seismic intensity scale (shown as "Intensity 6+ (Japan scale)").
4. Fragility curves by structure and era → probability of each damage level; a dice roll picks the outcome.
5. Validate: run many scenarios and compare against Tokyo's official damage estimate for Shinjuku (pitch line).
Damage levels: none / light / moderate / severe / collapsed. Collapses next to roads can block them.

## Building data (PLATEAU — already extracted)
5,335 buildings in the demo area. **Real:** height (96%), floors, usage, fire-resistance class (90%; tells wood from non-wood). **Inferred:** RC vs steel (from height/floors) and construction era — PLATEAU has no year built for Tokyo, so the simulator re-rolls era per run. Every inferred field is marked "inferred". Kobe-based fragility numbers run high for modern buildings; tune against Tokyo's estimate.
Files: `data/plateau/COVERAGE.md`, `data/plateau/REFERENCE.md` (formulas, constants, `damage_model.py`).

## Report channels
MVP: 119 emergency calls (audio), eyewitness texts, social media, fire/police radio. Later: hospital feeds, shelter updates, ward bulletins, sensors, responder field updates. Each has its own speed, accuracy, location precision, format and language; each report carries a source-reliability score. Code decides the facts, delays and errors; **Gemini only writes the human wording**; text-to-speech makes call audio.

## Agents
Logical roles: orchestrator, input, language, detection, medical-needs, verification, resource, planning, report, notifying. MVP implements them as ~4 Gemini calls (intake; verify/merge; planning; briefing); resource matching, routing, validation and DB writes are plain Python. Humans approve plans; approval is per plan version and a material change invalidates it.

## Data flow
Prep (load PLATEAU/OSM/facilities into Neon; build NetworkX graph from the same road IDs) → scenario starts (seed) → simulator writes truth → report generator releases reports over time → agents process each report **live** (intake → detect → verify/merge → update situations & road beliefs) → resource + routing → plan v1 + English briefing → human approval → test notification + ack → new event (e.g. road collapse) → plan v1 invalidated, reroute to v2, briefing explains why (**key demo moment**) → end: reveal truth + scorecard.
Live updates to the map over a WebSocket. Databricks gets a **batch export at the end of each scenario** (not live); live report streaming to agents is unaffected.

## Map
One MapLibre map, layers: OSM basemap → 3D PLATEAU buildings colored by damage → roads (open/limited/blocked) → incidents & resources → routes (v1 faded, v2 bold) → reveal-truth overlay. Building and road IDs on the map **must match** the database and NetworkX.
Working prototype: `demo/shinjuku-quake.html` (sources in `demo/source/`). Roads in the prototype are approximate; real build uses OSMnx roads.

## Database
Full table/column spec: `database/database-schema.md`.

## Team split (suggested)
A: backend/data (Neon, FastAPI, scenario runner, Databricks export). B: agents (Gemini prompts, Whisper). C: simulation physics + routing (OSMnx/NetworkX). D: frontend (React + MapLibre, panels, reveal truth).
Hours 0–3: agree on schema + message formats and stub endpoints. Goal by hour 20: one earthquake scenario running end to end in the UI.

## If behind, cut in this order
Extra channels → flood → audio beyond a few clips → fancy UI. Never cut: hidden truth vs reports, live map, reroute moment, human approval, reveal-truth scorecard.
