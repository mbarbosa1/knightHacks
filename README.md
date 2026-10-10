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

Edit `backend/.env` with one connection string per login, all for the **same
development branch** (see [Database logins](#database-logins)). Neon project:
`soft-mountain-91298548`, database: `neondb`.

The `dev` branch is `br-royal-flower-b4y1ucfi`; all logins in `backend/.env`
point at it. On October 10, 2026 its expiry was extended by one month (to about
November 10, 2026). Extend it again in Neon if needed. Do not silently fall
back to main.

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
  "road_graph_version": "YOUR_ROAD_GRAPH_VERSION",
  "seed": 42,
  "hazard_types": ["earthquake"],
  "simulator_version": "unity-dev"
}
```

An explicitly labeled manual development observation:

```json
{
  "id": "test-fixture-observation-001",
  "channel": "eyewitness_text",
  "format": "text",
  "original_text": "TEST FIXTURE: smoke reported near a building",
  "language": "en",
  "simulation_time_s": 10
}
```

## Database logins

`database/schema.sql` is the agreed design (`database/final-database.md`). It
creates the `static` and `ops` schemas, the tables, and three login roles with
their grants. Each program has its own login and its own variable in
`backend/.env`:

| Variable | Role | Read by |
|---|---|---|
| `DATABASE_URL` | `simulator` | the FastAPI backend |
| `MIGRATION_DATABASE_URL` | `neondb_owner` | `scripts/apply_schema.py` |
| `MAP_IMPORT_DATABASE_URL` | `map_import` | `scripts/load_static.py` |

The `agents` role is for the agent code, which keeps its own connection string.
Never put the owner login in `DATABASE_URL`.

The roles are created without passwords, and Neon's "Reset password" does not
work for roles created by SQL. Set each one in the Neon SQL Editor, as the
owner, on the same branch, when a program first needs it:

```sql
ALTER ROLE simulator WITH PASSWORD '<long random password>';
```

Generate passwords with `python3 -c "import secrets; print(secrets.token_hex(24))"`
and use a different one per role.

To rebuild the schema on a dev branch, run
`backend/.venv/bin/python scripts/apply_schema.py`. It drops `ops` (all run
data) and re-applies `schema.sql` in one transaction; `static` tables and role
passwords are kept, and the grants are re-applied.

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
schedule agent processing. `road_graph_version` is recorded but cannot be
validated against a graph registry because that registry does not exist yet.

References: [Neon connection pooling](https://neon.com/docs/connect/connection-pooling),
[Psycopg pool lifecycle](https://www.psycopg.org/psycopg3/docs/advanced/pool.html),
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/).
