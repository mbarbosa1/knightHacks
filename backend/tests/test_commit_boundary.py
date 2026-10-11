import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from psycopg import OperationalError
from app.config import Settings
from app.main import create_app
from live_check import request


class FailedCommitPool:
    async def open(self):
        pass

    async def close(self):
        pass

    @asynccontextmanager
    async def connection(self):
        yield AsyncMock()
        raise OperationalError('simulated commit failure')


class CommitBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_commit_failure_returns_error_before_success_is_sent(self):
        app = create_app(Settings(database_url='postgresql://placeholder'), FailedCommitPool())
        async with app.router.lifespan_context(app):
            response = await request(app, 'GET', '/health/ready', expected=503)
        self.assertEqual(response['detail'], 'Database unavailable')
