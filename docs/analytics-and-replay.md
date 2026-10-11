# Analytics & Replay

Use the **Analytics & Replay** button in the top navigation, or open `/analytics`.
Operations stays at `/`. Switching views keeps an already-open operational map
mounted; analytics never pauses, generates, dispatches, scores, or changes a run.

All analytics records come from Databricks `knighthacks.truth`,
`knighthacks.history`, and `knighthacks.eval`. They are not copied from Neon into
the UI as a fallback. The map plots locations present in exported reports and
incidents; truth rows do not contain map geometry.

## Connect Arwa's workspace

The supplied workspace ID is `7474645633187109`. The supplied sharing identifier
is `aws:us-east-2:09b44215-3934-46f5-bb5b-9825782672b2`. These identify the workspace
and metastore, but neither provides SQL access or authentication.

In Arwa's Databricks workspace, open **SQL Warehouses → the warehouse → Connection
details**. Configure these in the ignored `backend/.env` (preserve existing keys):

```
DATABRICKS_HOST=https://<Server-hostname>
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/<warehouse-id>
DATABRICKS_TOKEN=<authenticated-connection-token>
```

The identity needs warehouse query access, USE CATALOG / USE SCHEMA, and SELECT
on the KnightHacks tables. Keep the token server-side. Restart the backend after
configuration and refresh Analytics. Credentials are never returned to the UI.

Run `./dev.sh` as usual. If an earlier backend is already running on port 8000,
restart it to load the new routes. No database migrations are required.

## Replay and exports

The slider uses recorded event timestamps, independent of simulation speed.
Each table's state is reconstructed from its events, including deletions.
During replay, truth is limited to the last simulation timestamp found in the
visible event history. Events without a recorded timestamp are included only
in the latest view. Run-level scorecards always remain run-level scores.

History appears only after a run's operational events have been exported to
Databricks. Existing `sim.databricks_sink --events <run_id>` exports history and
`--score <run_id>` computes scores; this page only reads the results.

Data Explorer searches all fields, pages through records, displays complete
records, and exports every matching row and field to CSV. Formula-like text is
escaped for spreadsheet safety. Results over 50,000 rows or truncated warehouse
results return an explicit error instead of an incomplete replay.

Analytics is an operator view: do not register its truth routes as agent tools.
Use the repository's existing localhost development deployment until its broader
authentication/access controls are implemented.
