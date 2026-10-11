# Simulator → Neon pipeline: how to run and test it

How an earthquake simulation gets from Gerard's simulator into the Neon `ops` schema, how to check it
landed correctly, and what is still to do.

## The rule behind it

The simulator produces two kinds of data, and they go to different places:

| Data | What it is | Where it goes |
|---|---|---|
| **Hidden truth** | Building damage, road status, people, `truth_links` (the answer key) | Local files in `simulation/runs/<run_id>/` (Databricks later). **Never Neon.** |
| **Observations** | Noisy reports (119 calls, social posts, radio, eyewitness texts) | Neon `ops.observations`, through the backend API |

Agents read Neon. If truth reached Neon, agents could read the answer key and the reveal-truth
scorecard would be meaningless.

## How the simulator is plugged into the backend

`sim/api_storage.py` (`ApiStorage`) has the same methods as `LocalStorage`, but sends HTTP requests to
the FastAPI backend instead of writing files. The simulator never talks to Neon directly.

```
simulator                          FastAPI backend                        Neon
load_static_map()   GET  /map/versions/{v}/buildings|roads|facilities  ->  static.*
save_run()          POST /runs                                         ->  ops.simulation_runs ('created')
(damage, roads, people computed in memory = hidden truth)
save_truth_batch()  nothing sent; local files only
save_observation()  POST /runs/{id}/observations                       ->  ops.observations
                                                     first one flips the run to 'running'
end_run()           POST /runs/{id}/end                                ->  'completed', ended_at set
```

The backend does the validation (`models.py`), the SQL (`repository.py`) and logs every write to
`ops.operational_events`.

## Setup (once)

```bash
python3 -m venv .venv                                   # repo root
.venv/bin/pip install -r simulation/requirements.txt    # simulator packages (shapely, numpy, ...)
```

The backend has its own venv in `backend/.venv` and needs `backend/.env` with `DATABASE_URL`.

## The pipeline, step by step

Start the backend (restart it if it was started before these changes):

```bash
cd backend && .venv/bin/uvicorn app.asgi:app --port 8000
```

Then, in another terminal:

```bash
cd simulation
../.venv/bin/python scripts/pipeline_demo.py --seed 7 --mw 7.3
```

`scripts/pipeline_demo.py` runs the same steps as `sim.run.start_run`, printing each one:

1. **Map from Neon.** Downloads 5,335 buildings, 1,818 roads and 13 facilities from `static.*`.
2. **Event + hidden truth.** The seed becomes an earthquake (magnitude, depth, epicentre). Damage is
   computed for every building, debris blocks or limits roads, and people are placed in buildings.
   Nothing here leaves the simulator.
3. **Into `ops`.** Creates the run (`created`), then posts each report to `ops.observations`. The run
   becomes `running` on the first report. About 90 s for ~270 reports (one request each).
4. **Check against Neon** (from the repo root, see below).

The shorter version, without step-by-step output:

```bash
../.venv/bin/python -m sim.run --seed 7 --mw 7.3 --api http://127.0.0.1:8000 [--end]
```

`--mw` fixes the magnitude. Without it the seed picks one, and a small or distant quake can produce
almost no reports (seed 7 alone gives Mw 6.4 and a single report). `--end` marks the run completed,
after which the backend refuses all writes to it, the agents' included.

## Automatic: the map's "New earthquake" button

Gerard's simulator service (`sim/server.py`, from the `3DMAP` branch) stores every run in Neon when
`BACKEND_URL` is set. Each click is a new run with a new run uuid from Neon. Three servers:

```bash
cd backend && .venv/bin/uvicorn app.asgi:app --port 8000                                                # backend
cd simulation && BACKEND_URL=http://127.0.0.1:8000 ../.venv/bin/python -m uvicorn sim.server:app --port 8100   # simulator
cd frontend && npx vite --port 5173                                                                     # website
```

`frontend/.env` needs `VITE_API_BASE=http://127.0.0.1:8000` (the map loads from Neon; there are no local
map files without `simulation/data/`) and `VITE_SIM_BASE=http://127.0.0.1:8100`.

What one click does:
1. The website sends `POST /runs {"mw": ...}` to the simulator.
2. The simulator creates the run in `ops.simulation_runs` (Neon's uuid), computes the truth, and answers
   in ~3 s with the truth for the map (and the stand-in dispatcher's units, routed on a road graph built
   from Neon's `road_segments`).
3. After answering, it posts the reports to `ops.observations` in the background (~1.5-3 min). The run
   turns `running` on the first one. If posting fails, the run is marked `failed`.
4. Check it: `backend/.venv/bin/python scripts/check_sim_run.py --latest`

Without `BACKEND_URL` the simulator works as before, from local files only.

## Checking a run

```bash
backend/.venv/bin/python scripts/check_sim_run.py <run_id>            # from the repo root
backend/.venv/bin/python scripts/check_sim_run.py <run_id> --delete   # check, then remove it from Neon
```

It connects to Neon directly with psycopg (not through the simulator or the API) and compares it with
the simulator's local copy in `simulation/runs/<run_id>/`:

- the run row: seed, map version, hazard types, and the status it should have
- every observation, field by field, including the location point
- no observations missing from Neon, and none in Neon that the simulator did not produce
- every `truth_links` row points at an observation that exists in Neon
- the event log has one entry per observation plus the run's own entries
- notes any rows in `incidents`, `road_restrictions` or `decisions` (these should come from agents only)

It exits with code 1 if anything is wrong, so it can be used in scripts.

## Automated tests

```bash
cd simulation && ../.venv/bin/python -m pytest tests -q
```

`tests/test_api_storage.py` runs `ApiStorage` against a fake backend (no Neon needed):

| Test | Checks |
|---|---|
| `test_observation_body_matches_api_model` | `claimed_geom` becomes `location`; `run_id` and `received_at` are not sent |
| `test_run_id_comes_from_the_backend` | The simulator adopts the run id Neon generates; requests go to the right endpoints; `end_run` updates the local copy |
| `test_backend_errors_are_raised_with_detail` | A 4xx from the backend raises `ApiError` with the backend's message |

16 older tests need `simulation/data/` (the map files), which is not in the repo. They fail with
`FileNotFoundError` on machines without it; that is unrelated to this pipeline.

## Verified so far (10 Oct)

| Check | Result |
|---|---|
| Seed 42 into Neon | 253 observations, 0 errors |
| Seed 7, Mw 7.3 into Neon | 266 observations, 0 errors |
| Same observation sent twice | Stored row returned, no duplicate |
| Run status | `created` → `running` (first report) → `completed` (end) |
| Writing to an ended run | Refused with 409 |
| Same seed run twice | Works since observation ids include the run id (see below) |

Runs for seed 42 and seed 7 are still in Neon for inspection.

**Fixed: rerunning a seed gave 409 "Database reference or constraint conflict".** Observation ids came
only from the seed, so a second run of the same seed reused the first run's ids, and
`ops.observations.id` is unique across all runs. `sim/reports.py` now mixes the run id into each id
(`new_id()`): the same seed still gives the same reports, but every run gets its own ids. If a run
stops halfway, it stays in Neon as `created`; remove it with `check_sim_run.py <run_id> --delete`.

## What we are doing next

1. **Responder units in `ops.resources`.** Create ambulances / fire units / police units at the 13
   facilities with status `available`. The simulator owns this table; it later moves units through
   `available → en_route → busy`.
2. **Live loop (Gerard's step 8).** Release reports over time by `sim_time_s` instead of all at once,
   move units along routes, emit `responder_update` reports when a unit hits a blocked road, and
   schedule a road collapse mid-run (the reroute demo moment).
3. **Road graph from Neon.** `ApiStorage.load_static_map` returns no graph yet; routing needs one built
   from `road_segments`.
4. **Truth to Databricks.** Replace the local truth files with a Databricks writer
   (`knighthacks.truth.*`); needs `event_id` on `truth_links` and `scenarios.params` as a JSON string.
5. **Faster posting.** One HTTP request per report takes ~90 s per run. A bulk observations endpoint
   would help if it becomes a problem; the live loop will spread posts over time anyway.

Not planned: writing truth road statuses into `ops.road_restrictions`, or anything else into
`incidents` / `decisions`. Those are the agents' beliefs. A debug-only switch to copy true road status
for map testing is possible, but must stay off for real runs.
