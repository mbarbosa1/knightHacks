# Simulation ingestion and agent handoff

A successful Generate response now means the initial observations and responder
inventory have committed to Neon together. The backend transaction completes
before the HTTP success response is sent. No schema migration is required.

## Agent entry points

Given the run UUID returned by Generate:

1. `GET /runs/{run_id}/ingestion`: require `ready: true` before starting work.
   The response includes expected initial and current stored observation/resource
   counts. Older runs created before this feature have no receipt and are not
   automatically certified; generate a new run to use this handoff.
2. `GET /runs/{run_id}/observations?limit=100`: read reports, not truth or answers.
   Save your processing cursor durably. For the next page, supply
   `after=<last observation UUID>`. The feed orders by database received time
   and UUID; use `sim_time_s` to interpret simulated time within a snapshot.
3. `GET /runs/{run_id}/resources`: available modeled responder inventory.
4. Write beliefs through the existing incident/restriction endpoints. Those
   beliefs are deliberately not copied from simulator ground truth.

The feed is not an agent task queue or an exactly-once execution system. Agents
must persist their own processing acknowledgements/cursors and use stable IDs
for output retries. Caller authentication/per-agent authorization remains future
work; these services are still local development services.

## Save and recovery

- `POST /runs` accepts an optional stable client UUID, making run creation safe
  to retry after a lost response. Reusing it with different parameters conflicts.
- `POST /runs/{run_id}/ingest` accepts `{observations: [...], resources: [...]}`.
  Observation IDs are required. It validates and inserts reports, resources,
  their events, and a readiness receipt in one transaction. Any error rolls the
  submission back. It leaves the run open (`running`) for agent work.
- An identical snapshot retry returns the existing receipt and never resets
  resources that agents have subsequently changed. A different snapshot conflicts.
- The simulator writes a durable submission plus generated report spool under
  `simulation/runs/{run_id}` before attempting the batch. Prepared submissions
  are retried on simulator startup and through `POST /runs/{run_id}/retry`.
- Temporary connection errors are retried with the same IDs. Failed submissions
  return 503 and are retained for Retry saving this run in the UI.
- A process killed during generation can leave a `preparing` job and a created
  run. It is not marked ready and is not submitted as a partial snapshot. Generate
  a new run; automatic recovery applies only to complete `prepared` snapshots.
- Ground truth remains in simulator memory/local files; Databricks export has
  not been implemented by this change. Do not expose simulator truth endpoints
  to agents. `submission.json` and pending report files are local recovery data.

## Startup and UI

Restart backend and simulator after installing these changes. FastAPI >=0.121
is required for the commit-before-response dependency scope; the current local
backend already satisfies it.

`./dev.sh` now sets BACKEND_URL, VITE_API_BASE, and VITE_SIM_BASE explicitly and
requires the backend configuration. It does not depend on local map exports.
The simulator defaults to Neon mode; SIM_LOCAL_ONLY=1 is an explicit local demo,
which the frontend will not show as a successfully saved run.

The UI waits for a confirmed receipt, follows the new run for operational
polling, and remembers it for this browser tab. Initial unit counts follow the
existing simulator model: two units per hospital/fire station and one per police
facility. Unit capacity stays unknown/null. This is modeled inventory, not a
claim about actual facility staffing or capacity.

These guarantees cover committed API writes and recovery from process/network
failure while the local disk is available. They are not a backup of the entire
machine or a Databricks truth export.
