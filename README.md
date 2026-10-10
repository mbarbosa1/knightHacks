# KnightHacks / KYU-BOT

FastAPI connects to Neon Postgres + PostGIS. The simulator and the browser
(React + MapLibre) call the API;
database credentials stay server-side. The React/Vite frontend currently uses
mock reports, dispatches, and an illustrated placeholder map.

## Current status

Verified October 10, 2026 against the configured development database:

- Map baseline: 5,335 buildings, 1,818 road segments, 13 facilities.
- Source validation: zero errors; one warning that the road `highway` property
  is not imported because it is not part of the agreed schema.
- Backend: bulk GeoJSON, building/facility lookup, run lifecycle, observations,
  and resource/incident/road-restriction reads and writes.
- API login: `simulator`, with static read and operational write permissions.
- Verification: six contract/unit tests and a live API flow checking geometry,
  retries, run isolation, ended runs, and atomic event history. All test writes
  are rolled back.

Still to connect: the MapLibre 3D map and frontend API calls, the simulation,
agent processing, routing/replanning, decision approval, and Databricks
export/scoring. There is no simulator or agent/export implementation in
this repository. The API has no authentication or per-user authorization yet;
keep it on localhost until those are added.

## Start the backend

Python 3.11 or newer:

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

If `.env` does not exist, copy `.env.example` to `.env` and configure it.
Do not overwrite existing credentials. Start from `backend`:

```sh
uvicorn app.asgi:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/docs to try the endpoints. `/health/live` checks the
process; `/health/ready` checks connectivity and map-version/run table access.

## Map API

- `GET /map/versions`: available versions.
- `GET /map/versions/{id}`: version metadata.
- `GET /map/versions/{id}/buildings`: all footprints as a GeoJSON FeatureCollection.
- `GET /map/versions/{id}/roads`: all road lines as a GeoJSON FeatureCollection.
- `GET /map/versions/{id}/facilities`: facility records with GeoJSON `geometry`.
- `GET /map/versions/{id}/buildings/{building_id}`: building and nearby facilities;
  optional `radius_m` (default 1000 meters) and `limit` (default 50).

Both bulk endpoints accept `?bbox=west,south,east,north` in longitude/latitude
(EPSG:4326). Omit bbox for the whole map. Invalid boxes return 422, unknown
versions 404, empty regions an empty FeatureCollection. Geometry is not clipped.
Each feature has `id`, `geometry`, and `properties`. Buildings include
`height_m`, floors, source IDs, facility tags and other static attributes.
Missing heights stay null: the renderer must choose a display fallback.
Road `from_node`/`to_node` values are strings to preserve bigint precision.
Responses support gzip when requested by the client.

Load static map layers once per pinned version and refresh operational state
separately. `src/types/database.ts` describes database rows, not these API
projections: bulk layers use `geometry`/`properties`, single lookups use
`geometry` or `location`, and public runs omit `seed`. Add an API adapter when
connecting the frontend instead of blindly casting database interfaces.

## Simulator API

Create a run with `POST /runs` (use a version returned by `/map/versions`):

```json
{"map_version_id":"shinjuku-v1","seed":42,"hazard_types":["earthquake"]}
```

Save the returned UUID. Creation is not idempotent. `GET /runs/{run_id}` returns
public run context. The first new observation changes status to `running`.

Save a report with `POST /runs/{run_id}/observations`:

```json
{
  "id":"eb496a12-f33e-4d3b-a846-070c285ab46a",
  "channel":"eyewitness_text",
  "format":"text",
  "original_text":"TEST FIXTURE: smoke reported near a building",
  "lang":"en",
  "location":{"longitude":139.7,"latitude":35.7},
  "sim_time_s":10
}
```

Always supply a stable UUID for retry safety. Without `id`, each submission
creates a new report. Identical retries return the existing row; changing any
persisted field returns 409. Read using
`GET /runs/{run_id}/observations/{observation_id}`.

Use a stable simulator-chosen UUID for resources/incidents:

- `PUT /runs/{run_id}/resources/{resource_id}`: create or replace resource state.
- `PUT /runs/{run_id}/incidents/{incident_id}`: create or replace incident state.
- `PUT /runs/{run_id}/road-restrictions/{road_id}`: create or replace road state.
- `POST /runs/{run_id}/end`: `{"status":"completed"}` or `{"status":"failed"}`;
  optionally include `sim_time_s`. Server sets `ended_at`. Repeating the same
  ending preserves that time; a different terminal status returns 409.
- `GET /runs/{run_id}/resources`, `/incidents`, `/road-restrictions`: current state;
  optional `limit` (default 100, maximum 1000) and `offset`.

PUT replaces full state, not just supplied fields. Send all fields you want to
preserve; omitted optional values reset to null/default. Identical state retries
leave timestamps and events unchanged. After ending, new observations and all
state PUTs return 409; exact observation retries still return the saved report.
A resource/incident UUID cannot be reassigned to another run.

Resource example:

```json
{"type":"ambulance","name":"Ambulance 1","status":"available","capacity":4,
 "skills":["medical"],"location":{"longitude":139.7,"latitude":35.7},
 "home_facility_id":null,"sim_time_s":10}
```

Incident example:

```json
{"status":"reported","building_id":null,"uncertainty_m":25,
 "location":{"longitude":139.7,"latitude":35.7},
 "person_groups":[{"group_id":"group-1","est_count":3,"needs":["medical"]}],
 "evidence":{"supports":[],"contradicts":[]},"sim_time_s":10}
```

Road restriction example:

```json
{"status":"BLOCKED","source_observation_id":null,"sim_time_s":10}
```

Use building/facility/road IDs from the run's map version. Evidence/source UUIDs
must reference observations in the same run. Invalid references return 422.
Set a reopened road to `OPEN`. Restriction storage does not yet recalculate
routes, invalidate decisions, or schedule agents.

## Event history and simulation time

The v1 schema has reference-validation triggers, **no logging triggers**.
The API appends `ops.operational_events` in the same transaction as each
changed row. Events have stable UUIDs and payloads with `operation`, a public
row snapshot, and optional `sim_time_s`. No-op PUTs and exact observation/end
retries append nothing. Programs writing directly to SQL must maintain their
own outbox or later use shared logging triggers; API logging covers API writes.

Only observations have a dedicated `sim_time_s` column. Other write bodies can
supply simulation time for event history, but current-state tables have no
simulation clock/version columns. Updates follow arrival order; older simulator
frames are not rejected. Add sequencing through a migration before concurrent
or out-of-order writers require that guarantee. These endpoints require no
schema changes.

## Database logins and scripts

All URLs in ignored `backend/.env` belong to the same development branch.
Neon project: `soft-mountain-91298548`; database: `neondb`.

- `DATABASE_URL`: pooled `simulator` login used by FastAPI.
- `MIGRATION_DATABASE_URL`: direct owner login for `scripts/apply_schema.py`.
- `MAP_IMPORT_DATABASE_URL`: `map_import` login for `scripts/load_static.py`.

The `agents` role is separate. Keep owner credentials out of `DATABASE_URL`,
the simulator, and the browser. `database/schema.sql` matches `database/final-database.md`.
**`scripts/apply_schema.py` deletes all ops data** before rebuilding it, with a
confirmation prompt. It is not an incremental migration runner; do not run it
to deploy API-only changes.

`load_static.py` loads `data/buildings.geojson`, `data/facilities.geojson`, and
optional `data/road_segments.geojson` transactionally. It is not fingerprint-
idempotent: repeat versions conflict, and `--replace` deletes/reloads a version
rather than creating an immutable new one. Preserve the imported baseline for
active runs.

## Verification

From the repository root:

```sh
backend/.venv/bin/python -B scripts/validate.py
cd backend
.venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/python -B tests/live_check.py --env-file .env
```

The live check uses the real simulator login/imported map, exercises FastAPI
in process, and rolls back all fixtures. It needs network access to Neon.
It does not test the simulator, the MapLibre map, agents, routes, or Databricks.

Frontend: `npm install`, then `npm run dev` or `npm run build`. Reports, map,
and dispatch controls currently remain local demo UI state.
