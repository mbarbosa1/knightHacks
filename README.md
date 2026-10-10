# KnightHacks backend starter

The API connects to the existing Postgres database on Neon. The browser and
Unity call FastAPI; database credentials stay in the backend.

## Local setup

Python 3.11 or newer:

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `backend/.env` with a pooled connection for a restricted backend role and
a direct owner/migration connection for the **same development branch**. The
API only reads `DATABASE_URL`; it never uses the migration credentials. Neon
project: `soft-mountain-91298548`, database: `neondb`.

The existing `dev` branch is `br-royal-flower-b4y1ucfi`. It expires October 11,
2026 at 5:22 AM Eastern. Extend its lifetime in Neon or select a longer-lived
development branch before relying on it. Do not silently fall back to main.

```sh
uvicorn app.asgi:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/docs to try the API. This starter is for local
development: it has no authentication or per-user authorization yet. Keep it
bound to localhost until those are added.

## What works in this first step

- `GET /health/live`: process health.
- `GET /health/ready`: connection and required-table access.
- `GET /map/versions`: available imported map versions (bounded list).
- `GET /map/versions/{version_id}`: version metadata.
- `GET /map/versions/{version_id}/buildings/{building_id}`: building geometry
  and nearby facilities; `radius_m` defaults to 1000 meters, `limit` to 50.
- `POST /runs`: start a run pinned to a known map version.
- `GET /runs/{run_id}`: public run context, excluding seed and parameters.
- `POST /runs/{run_id}/observations`: save a simulator report atomically.
- `GET /runs/{run_id}/observations/{observation_id}`: retrieve a report.
- `GET /runs/{run_id}/incidents` and `/resources`: run-scoped, paginated lists.

http://127.0.0.1:8000/docs gives you a clickable page where you can try every endpoint

The same observation ID and payload can be retried without duplicate writes.
A changed payload for that ID returns 409. New observations on an ended run
return 409. Delayed observations keep their original simulation time and do
not move the run clock backwards. Database triggers log writes automatically.

Run creation body after a real map has been imported:

```json
{
  "map_version_id": "YOUR_IMPORTED_MAP_VERSION",
  "seed": 42,
  "hazard_types": ["earthquake"]
}
```

An explicitly labeled manual development observation:

```json
{
  "id": "00000000-0000-4000-8000-000000000001",
  "channel": "eyewitness_text",
  "format": "text",
  "original_text": "TEST FIXTURE: smoke reported near a building",
  "lang": "en",
  "sim_time_s": 10
}
```

## Database baseline and permissions

`database/001` and `002` preserve the initial schema scripts from the project.
The tables and event triggers already exist on main and dev. **Do not replay
these scripts on those branches.** They are historical baseline files, not
startup tasks. There is no automatic migration runner yet.

`database/003_backend_permissions.sql` contains grants for this starter's
endpoints. It has not been applied. Provision a plain LOGIN role named
`knighthacks_backend` with a password through secure administrative tooling
first. Ensure it has no privileged role memberships, ownership, schema CREATE
rights, or additional grants. Role creation through Neon management tools can
give broader access; inspect memberships before using such a role in the API.
Apply the grants using the direct owner connection on your dev branch.

The API role reads static map tables, writes runs/observations, reads
incidents/resources, and inserts trigger-generated operational events. It
cannot be treated as restricted until its effective privileges are checked.
Importer and simulator/export roles remain separate future setup work.

## Verification

```sh
cd backend
python -m unittest discover -s tests -v
```

These tests cover input validation, retry conflicts, ended runs, and delayed
observations. Database integration requires installed dependencies, configured
credentials, and actual imported map data. Readiness does not certify all write
permissions; check a transaction on a development branch after provisioning.

## Next step: real map import

No map source files were present when this starter was created, and Neon had
zero map versions, buildings, roads, facilities, and runs. The building lookup
therefore returns 404 until import. Source fields and CRS must be inspected
before writing the importer; do not infer coordinates or substitute fixtures
for real map data.

Provide `buildings_table.geojson`, the road graph, and the facility dataset in
the ignored `data/` folder or identify their existing location. The importer
will validate IDs, CRS, era labels/probabilities, and the 12 expected facility
building links; preserve nulls and source fields; and write all map records in
one transaction. A source fingerprint will make map versions safe to rerun.

After import, prove the building/facility lookup against real data. Then add
incident updates, planning tools, decision validation/version approval, agent
execution, and Databricks export. Observation storage currently does not
schedule agent processing. `road_graph_version` is not stored yet: the
simulation_runs table has no column for it.

References: [Neon connection pooling](https://neon.com/docs/connect/connection-pooling),
[Psycopg pool lifecycle](https://www.psycopg.org/psycopg3/docs/advanced/pool.html),
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/).
