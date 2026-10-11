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
    ../.venv/bin/python -m sim.databricks_sink --events <run_id>   # what the agents did -> history.simulation_events
    ../.venv/bin/python -m sim.databricks_sink --score <run_id> [agent_config]   # reveal truth -> eval.scorecards

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
With any of them missing (or DATABRICKS_UPLOAD=off) the upload is off and the simulator works exactly as before.
'''
import concurrent.futures, datetime, json, logging, os, pathlib, random, ssl, sys, time, urllib.error, urllib.request, uuid
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

def _env_file():
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                values[k.strip()] = v.strip()
    return values


def load_settings():
    """{name: value} for the three settings, or None when any is missing (upload off)."""
    file_values = _env_file()
    settings = {k: os.environ.get(k) or file_values.get(k) for k in SETTINGS}
    return settings if all(settings.values()) else None


def enabled():
    """Upload on when the three settings exist. DATABRICKS_UPLOAD=off turns it off (the tests set it, so test
    runs never reach the real tables)."""
    return os.environ.get('DATABRICKS_UPLOAD', '').lower() != 'off' and load_settings() is not None


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

def merge_sql(table, key, cols, schema='truth'):
    """MERGE that inserts the rows of the :rows JSON parameter whose key is not in the table yet."""
    # Timestamps arrive as ISO strings; everything else parses straight to its type.
    struct = ', '.join(f'`{c}`: {"STRING" if t == "TIMESTAMP" else t}' for c, t in cols.items())
    select = ', '.join(f'CAST(r.`{c}` AS TIMESTAMP) AS `{c}`' if t == 'TIMESTAMP' else f'r.`{c}` AS `{c}`'
                       for c, t in cols.items())
    return (f'MERGE INTO knighthacks.{schema}.{table} t '
            f'USING (SELECT {select} FROM (SELECT explode(from_json(:rows, \'ARRAY<STRUCT<{struct}>>\')) AS r)) s '
            f'ON t.`{key}` = s.`{key}` WHEN NOT MATCHED THEN INSERT *')


def read_rows(path, key, cols):
    """Rows of one .jsonl file, only the table's columns, one per key (the last one wins).
    Runs written before truth_links had event_id get one derived from the row, so re-uploads still dedupe."""
    rows = {}
    with path.open() as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                row = {c: r.get(c) for c in cols}
                if row.get(key) is None and key == 'event_id':
                    row[key] = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(row, sort_keys=True)))
                rows[row[key]] = row
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
            new_rows[table] = _merge_chunks(db, merge_sql(table, key, cols), read_rows(path, key, cols))
    except Exception as exc:
        _write_status(run_dir, 'failed', new_rows, str(exc))
        raise
    _write_status(run_dir, 'uploaded', new_rows)
    return new_rows


def _merge_with_retry(db, sql, parameters, attempts=6):
    """A MERGE reads the whole table, so another writer appending at the same time (a second simulator,
    --pending, a teammate) aborts it with a Delta concurrency conflict. MERGE on the key is idempotent: retry."""
    for attempt in range(attempts):
        try:
            return db.sql(sql, parameters)
        except DatabricksError as exc:
            if 'CONCURRENT' not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(min(30, 2 ** attempt) + random.random())


def _merge_chunks(db, sql, rows):
    """Send rows in CHUNK-sized MERGEs; returns how many were new."""
    inserted = 0
    for i in range(0, len(rows), CHUNK):
        r = _merge_with_retry(db, sql, [{'name': 'rows', 'type': 'STRING', 'value': json.dumps(rows[i:i + CHUNK], ensure_ascii=False)}])
        data = (r.get('result') or {}).get('data_array') or [[0]]
        inserted += int(data[0][-1] or 0)   # MERGE returns (affected, updated, deleted, inserted)
    return inserted


# =============================================================================
# 3b. AGENT HISTORY: Neon ops.operational_events -> Databricks history.simulation_events
#     Everything agents (and the simulator / operator) changed during a run, for scoring.
#     Reads Neon with the simulator login (DATABASE_URL in backend/.env), which may read the outbox.
# =============================================================================

EVENT_COLUMNS = {'event_id': 'STRING', 'run_id': 'STRING', 'table_name': 'STRING', 'row_id': 'STRING',
                 'payload': 'STRING', 'created_at': 'TIMESTAMP'}


def neon_events(run_id):
    """The run's ops.operational_events rows, oldest first, shaped like history.simulation_events."""
    import psycopg                       # pip install "psycopg[binary]" (agents/requirements.txt has it)
    url = os.environ.get('DATABASE_URL') or _env_file().get('DATABASE_URL')
    if not url:
        raise DatabricksError(f'DATABASE_URL (Neon simulator login) is not set in {ENV_FILE}')
    with psycopg.connect(url) as conn:
        rows = conn.execute('SELECT event_id, run_id, table_name, row_id, payload, created_at '
                            'FROM ops.operational_events WHERE run_id = %s ORDER BY created_at, event_id',
                            (str(run_id),)).fetchall()
    return [{'event_id': str(e), 'run_id': str(r), 'table_name': t, 'row_id': i,
             'payload': json.dumps(p, ensure_ascii=False) if p is not None else None, 'created_at': c.isoformat()}
            for e, r, t, i, p, c in rows]


def export_events(run_id, db=None):
    """Copy one run's Neon event log to Databricks. Safe to repeat (MERGE on event_id): run it again
    after the agents did more work and only the new events are added. Returns (events in Neon, new)."""
    db = db or Databricks(load_settings())
    rows = neon_events(run_id)
    return len(rows), _merge_chunks(db, merge_sql('simulation_events', 'event_id', EVENT_COLUMNS, 'history'), rows)


# =============================================================================
# 3c. SCORECARD: agents' beliefs vs the truth -> eval.scorecards (databricks/scorecard.sql)
# =============================================================================

SCORECARD_SQL = pathlib.Path(__file__).resolve().parents[2] / 'databricks' / 'scorecard.sql'


def default_agent_config():
    """Which setup is being scored: the models the agents used (backend/.env): LLM_MODEL for the in_agent, and
    AGENT_LLM_MODEL for the tool-using agents when they run on a hosted endpoint, e.g.
    'qwen3-vl-8b-instruct+qwen3-vl-30b-a3b-instruct'."""
    env = _env_file()
    names = [os.environ.get(k) or env.get(k) for k in ('LLM_MODEL', 'AGENT_LLM_MODEL')]
    names = [n.split('/')[-1].lower() for n in names if n]
    return '+'.join(dict.fromkeys(names)) or 'unknown-model'


def score_run(run_id, agent_config=None, db=None):
    """Copy the run's latest event log, then (re)score it. Scoring again replaces this run's earlier
    rows for the same agent_config. Returns [(metric, value, details)]."""
    db = db or Databricks(load_settings())
    agent_config = agent_config or default_agent_config()
    export_events(run_id, db)
    params = [{'name': 'run_id', 'value': str(run_id), 'type': 'STRING'},
              {'name': 'agent_config', 'value': agent_config, 'type': 'STRING'}]
    db.sql('DELETE FROM knighthacks.eval.scorecards WHERE scenario_id = :run_id AND agent_config = :agent_config', params)
    db.sql(SCORECARD_SQL.read_text(), params)
    rows = db.sql('SELECT metric, value, details FROM knighthacks.eval.scorecards '
                  'WHERE scenario_id = :run_id AND agent_config = :agent_config ORDER BY metric', params)
    return [tuple(r) for r in (rows.get('result') or {}).get('data_array') or []]


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
    if not argv or len(argv) > {'--events': 2, '--score': 3}.get(argv[0], 1):
        sys.exit(__doc__)
    settings = load_settings()
    if not settings:
        sys.exit(f'Databricks is not configured: set {", ".join(SETTINGS)} (environment or {ENV_FILE})')
    if argv[0] == '--events':
        total, new = export_events(argv[1], Databricks(settings))
        sys.exit(print(f'{argv[1]}  {total} events in Neon, {new} new in Databricks history.simulation_events'))
    if argv[0] == '--score':
        config = argv[2] if len(argv) > 2 else default_agent_config()
        print(f'Scoring {argv[1]} as agent_config {config!r} (event log copied first)...')
        for metric, value, details in score_run(argv[1], config, Databricks(settings)):
            print(f'  {metric:<22} {"-" if value is None else round(float(value), 3):<8} {details}')
        sys.exit(0)
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
