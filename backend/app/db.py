from fastapi import Request
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import Settings
'''
Actual link to NEON CLOUD DB
Creates a connection pool (a few connections to Neon that stay 
open and get reused) and hands one to each request. It commits 
if the request succeeds and rolls back if it fails.
'''

def make_pool(settings: Settings) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        open=False,
        min_size=0,
        max_size=settings.db_pool_max_size,
        timeout=10,
        kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 10},
    )


async def get_connection(request: Request):
    # Commit on success, roll back on error; triggers share this transaction.
    async with request.app.state.pool.connection() as connection:
        yield connection
