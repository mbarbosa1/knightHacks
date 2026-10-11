'''
Copies each run's hidden truth from simulation/runs/<run_id>/ into Databricks (catalog knighthacks,
schema truth; tables in databricks/tables.sql), so the reveal-truth scorecard can compare the agents'
results with what really happened.

Automatic: sim/server.py calls upload_in_background() after every new earthquake, and
upload_pending_in_background() at startup to retry runs that failed or were never uploaded.
Manual:
    cd simulation
    ../.venv/bin/python -m sim.databricks_sink <run_id>     # one run
    ../.venv/bin/python -m sim.databricks_sink --pending    # every run not uploaded yet
    ../.venv/bin/python -m sim.databricks_sink --all        # every run (safe: existing rows are skipped)

How it works:
  - Truth stays on disk first (LocalStorage). This module only reads those .jsonl files, so a
    Databricks outage never loses data or breaks a run; the upload is retried later.
  - Each table is a MERGE on its key (event_id, or id for scenarios): re-uploading inserts nothing twice.
  - Rows go as one JSON parameter per chunk and from_json parses them in Databricks, so no values are
    pasted into SQL text. Chunks of 500 rows take ~3 s; 2000-row chunks hung for minutes.
  - After each attempt, runs/<run_id>/databricks_upload.json records the result:
        {"status": "uploaded" | "failed", "at": ..., "new_rows": {table: n}, "error": ...}
  - Uses the SQL Statement Execution REST API with the standard library (+ certifi for certificates).

Configuration (environment variables, else backend/.env; backend/.env is never committed):
    DATABRICKS_HOST       https://dbc-....cloud.databricks.com
    DATABRICKS_HTTP_PATH  /sql/1.0/warehouses/<id>
    DATABRICKS_TOKEN      dapi...
With any of them missing the upload is off and the simulator works exactly as before.
'''
import concurrent.futures, datetime, json, logging, os, pathlib, ssl, sys, time, urllib.error, urllib.request
from .storage import LocalStorage

log = logging.getLogger('uvicorn.error')

ENV_FILE = pathlib.Path(__file__).resolve().parents[2] / 'backend' / '.env'
SETTINGS = ('DATABRICKS_HOST', 'DATABRICKS_HTTP_PATH', 'DATABRICKS_TOKEN')
STATUS_FILE = 'databricks_upload.json'
CHUNK = 500

# table -> (merge key, columns as name: Databricks type). Must match databricks/tables.sql.
TABLES = {
    'scenarios': ('id', {
        'id': 'STRING', 'seed': 'BIGINT', 'hazard_types': 'ARRAY<STRING>', 'params': 'STRING',
        'map_version_id': 'STRING', 'simulator_version': 'STRING',
        'started_at': 'TIMESTAMP', 'ended_at': 'TIMESTAMP'}),
    'truth_building_damage': ('event_id', {
        'event_id': 'STRING', 'scenario_id': 'STRING', 'building_id': 'STRING', 'intensity': 'DOUBLE',
        'damage_level': 'STRING', 'simulation_time_s': 'DOUBLE', 'sampled_attributes': 'STRING'}),
    'truth_road_status': ('event_id', {
        'event_id': 'STRING', 'scenario_id': 'STRING', 'road_id': 'STRING', 'status': 'STRING',
        'cause': 'STRING', 'simulation_time_s': 'DOUBLE'}),
    'truth_people': ('event_id', {
        'event_id': 'STRING', 'id': 'STRING', 'scenario_id': 'STRING', 'building_id': 'STRING',
        'count': 'INT', 'condition': 'STRING', 'mobility': 'STRING', 'language': 'STRING',
        'simulation_time_s': 'DOUBLE'}),
    'truth_links': ('event_id', {
        'event_id': 'STRING', 'scenario_id': 'STRING', 'observation_id': 'STRING',
        'truth_entity_type': 'STRING', 'truth_entity_id': 'STRING',
        'truth_simulation_time_s': 'DOUBLE', 'match_type': 'STRING'}),
}


# =============================================================================
# 1. CONFIGURATION
# =============================================================================

def load_settings():
    """{name: value} for the three settings, or None when any is missing (upload off)."""
    file_values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                file_values[k.strip()] = v.strip()
    settings = {k: os.environ.get(k) or file_values.get(k) for k in SETTINGS}
    return settings if all(settings.values()) else None


def enabled():
    return load_settings() is not None


# =============================================================================
# 2. DATABRICKS CLIENT (SQL Statement Execution API)
# =============================================================================

def _ssl_context():
    # python.org Python on macOS ships without trusted root certificates; certifi has them.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class DatabricksError(RuntimeError):
    pass


class Databricks:
    def __init__(self, settings):
        self.host = settings['DATABRICKS_HOST'].rstrip('/')
        self.warehouse = settings['DATABRICKS_HTTP_PATH'].rstrip('/').rsplit('/', 1)[1]
        self.headers = {'Authorization': 'Bearer ' + settings['DATABRICKS_TOKEN'],
                        'Content-Type': 'application/json'}
        self.ssl = _ssl_context()

    def _request(self, method, path, body=None):
        req = urllib.request.Request(self.host + path, json.dumps(body).encode() if body else None,
                                     self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120, context=self.ssl) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise DatabricksError(f'HTTP {e.code}: {e.read()[:300]!r}') from None
        except urllib.error.URLError as e:
            raise DatabricksError(f'cannot reach Databricks: {e.reason}') from None

    def sql(self, statement, parameters=()):
        """Run one statement and wait for it (the serverless warehouse may need a moment to wake up)."""
        r = self._request('POST', '/api/2.0/sql/statements', {
            'warehouse_id': self.warehouse, 'statement': statement, 'wait_timeout': '50s',
            'on_wait_timeout': 'CONTINUE', 'parameters': list(parameters)})
        while r['status']['state'] in ('PENDING', 'RUNNING'):
            time.sleep(2)
            r = self._request('GET', f'/api/2.0/sql/statements/{r["statement_id"]}')
        if r['status']['state'] != 'SUCCEEDED':
            raise DatabricksError(f'SQL {r["status"]["state"]}: {r["status"].get("error")}')
        return r


# =============================================================================
# 3. UPLOAD ONE RUN
# =============================================================================

def merge_sql(table, key, cols):
    """MERGE that inserts the rows of the :rows JSON parameter whose key is not in the table yet."""
    # Timestamps arrive as ISO strings; everything else parses straight to its type.
    struct = ', '.join(f'`{c}`: {"STRING" if t == "TIMESTAMP" else t}' for c, t in cols.items())
    select = ', '.join(f'CAST(r.`{c}` AS TIMESTAMP) AS `{c}`' if t == 'TIMESTAMP' else f'r.`{c}` AS `{c}`'
                       for c, t in cols.items())
    return (f'MERGE INTO knighthacks.truth.{table} t '
            f'USING (SELECT {select} FROM (SELECT explode(from_json(:rows, \'ARRAY<STRUCT<{struct}>>\')) AS r)) s '
            f'ON t.`{key}` = s.`{key}` WHEN NOT MATCHED THEN INSERT *')


def read_rows(path, key, cols):
    """Rows of one .jsonl file, only the table's columns, one per key (the last one wins)."""
    rows = {}
    with path.open() as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                rows[r[key]] = {c: r.get(c) for c in cols}
    return list(rows.values())


def upload_run(run_dir, db=None):
    """Upload every truth file of one run and record the result. Returns {table: new rows}.
    Raises DatabricksError on failure (after recording it)."""
    run_dir = pathlib.Path(run_dir)
    db = db or Databricks(load_settings())
    new_rows = {}
    try:
        for table, (key, cols) in TABLES.items():
            path = run_dir / f'{table}.jsonl'
            if not path.exists():
                continue
            rows, sql, new_rows[table] = read_rows(path, key, cols), merge_sql(table, key, cols), 0
            for i in range(0, len(rows), CHUNK):
                r = db.sql(sql, [{'name': 'rows', 'type': 'STRING',
                                  'value': json.dumps(rows[i:i + CHUNK], ensure_ascii=False)}])
                data = (r.get('result') or {}).get('data_array') or [[0]]
                new_rows[table] += int(data[0][-1] or 0)   # MERGE returns (affected, updated, deleted, inserted)
    except Exception as exc:
        _write_status(run_dir, 'failed', new_rows, str(exc))
        raise
    _write_status(run_dir, 'uploaded', new_rows)
    return new_rows


def _write_status(run_dir, status, new_rows, error=None):
    (run_dir / STATUS_FILE).write_text(json.dumps({
        'status': status, 'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'new_rows': new_rows, 'error': error}, indent=1))


def pending_runs(runs_dir=None):
    """Run folders with truth files whose last upload did not succeed (or never happened)."""
    runs_dir = pathlib.Path(runs_dir or LocalStorage().runs_dir)
    pending = []
    for d in sorted(runs_dir.iterdir()) if runs_dir.exists() else []:
        if not (d / 'truth_building_damage.jsonl').exists():
            continue
        status = d / STATUS_FILE
        if not status.exists() or json.loads(status.read_text()).get('status') != 'uploaded':
            pending.append(d)
    return pending


# =============================================================================
# 4. BACKGROUND UPLOADS (used by sim/server.py)
#    One worker thread, so uploads run one at a time and never slow down POST /runs.
# =============================================================================

_worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='databricks')


def _upload_logged(run_dir):
    try:
        new_rows = upload_run(run_dir)
        log.info('Databricks: run %s uploaded %s', run_dir.name, new_rows)
    except Exception as exc:
        log.warning('Databricks: run %s not uploaded (%s). It will be retried at the next simulator start, '
                    'or run: python -m sim.databricks_sink --pending', run_dir.name, exc)


def upload_in_background(run_id):
    """Queue one run's upload. Does nothing when Databricks is not configured."""
    if enabled():
        _worker.submit(_upload_logged, pathlib.Path(LocalStorage().runs_dir) / str(run_id))


def upload_pending_in_background():
    """Queue every run that is not uploaded yet (called once at simulator start)."""
    if enabled():
        for run_dir in pending_runs():
            _worker.submit(_upload_logged, run_dir)


# =============================================================================
# 5. COMMAND LINE
# =============================================================================

def main(argv):
    if len(argv) != 1:
        sys.exit(__doc__)
    settings = load_settings()
    if not settings:
        sys.exit(f'Databricks is not configured: set {", ".join(SETTINGS)} (environment or {ENV_FILE})')
    runs_dir = pathlib.Path(LocalStorage().runs_dir)
    if argv[0] == '--pending':
        run_dirs = pending_runs(runs_dir)
    elif argv[0] == '--all':
        run_dirs = [d for d in sorted(runs_dir.iterdir()) if (d / 'truth_building_damage.jsonl').exists()]
    else:
        run_dirs = [runs_dir / argv[0]]
        if not run_dirs[0].is_dir():
            sys.exit(f'No run folder: {run_dirs[0]}')
    db, failed = Databricks(settings), 0
    for run_dir in run_dirs:
        try:
            print(f'{run_dir.name}  uploaded, new rows: {upload_run(run_dir, db)}')
        except Exception as exc:
            failed += 1
            print(f'{run_dir.name}  FAILED: {exc}')
    print(f'{len(run_dirs) - failed} of {len(run_dirs)} runs uploaded')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main(sys.argv[1:])
