"""Replace the old tables with the team's agreed design (database/schema.sql).

usage (from the project root):  backend/.venv/bin/python scripts/apply_schema.py

Runs database/004_drop_old_design.sql then database/schema.sql in ONE transaction
on MIGRATION_DATABASE_URL (the owner login) from backend/.env or the environment,
using the direct (non-pooler) host. The API never reads this variable.
Any error rolls everything back. Deletes all data in ops; static tables, roles
and role passwords are kept, and the grants are re-applied.
"""
import os, pathlib, sys
from urllib.parse import urlparse

import psycopg
from dotenv import dotenv_values

ROOT = pathlib.Path(__file__).resolve().parent.parent

url = os.environ.get('MIGRATION_DATABASE_URL') or dotenv_values(ROOT / 'backend' / '.env').get('MIGRATION_DATABASE_URL')
if not url:
    sys.exit('MIGRATION_DATABASE_URL is not set in backend/.env (use the Neon owner login)')
url = url.replace('-pooler.', '.', 1)
parts = urlparse(url)
if input(f'Drop ALL ops data and re-apply schema.sql on {parts.hostname} '
         f'as {parts.username}? Type yes: ') != 'yes':
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
