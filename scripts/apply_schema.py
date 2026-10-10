"""Replace the old tables with the team's agreed design (database/schema.sql).

usage (from the project root):  backend/.venv/bin/python scripts/apply_schema.py

Runs database/004_drop_old_design.sql then database/schema.sql in ONE transaction
on the DATABASE_URL from backend/.env, using the direct (non-pooler) host.
Any error rolls everything back. Deletes all data in "staticMapData" and ops.
"""
import pathlib, sys
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))
import psycopg  # noqa: E402
from app.config import Settings  # noqa: E402

url = Settings().database_url.get_secret_value().replace('-pooler.', '.', 1)
host = urlparse(url).hostname
if input(f'Replace ALL tables in "staticMapData" and ops on {host}? Type yes: ') != 'yes':
    sys.exit('cancelled, nothing changed')

sql = '\n'.join((ROOT / 'database' / name).read_text()
                for name in ('004_drop_old_design.sql', 'schema.sql'))
with psycopg.connect(url, connect_timeout=20) as conn:  # commits on success, rolls back on error
    conn.execute(sql)
    tables = conn.execute("SELECT schemaname || '.' || tablename FROM pg_tables "
                          "WHERE schemaname IN ('static', 'ops') ORDER BY 1").fetchall()
    roles = conn.execute("SELECT rolname FROM pg_roles WHERE rolname IN "
                         "('map_import', 'simulator', 'agents') ORDER BY 1").fetchall()
print(f'done: {len(tables)} tables', [t[0] for t in tables])
print('roles:', [r[0] for r in roles])
