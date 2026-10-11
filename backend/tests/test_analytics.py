import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.analytics import Warehouse, parse_record
from app.config import Settings
from app.main import create_app


class TestClient:
    """Tiny ASGI harness; avoids adding a second HTTP client dependency."""
    def __init__(self, app):
        self.app = app

    def get(self, path):
        async def invoke():
            messages = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                messages.append(message)
            await self.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
                "query_string": b"", "root_path": "", "headers": [],
                "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8000)}, receive, send)
            class Response:
                status_code = next(m["status"] for m in messages if m["type"] == "http.response.start")
                text = b"".join(m.get("body", b"") for m in messages).decode()
                def json(self):
                    return json.loads(self.text)
            return Response()
        return asyncio.run(invoke())


class AnalyticsTests(unittest.TestCase):
    def test_status_never_exposes_token(self):
        settings = Settings(_env_file=None, database_url="postgresql://placeholder",
                            databricks_host="https://example.cloud.databricks.com",
                            databricks_http_path="/sql/1.0/warehouses/test", databricks_token="private-token")
        response = TestClient(create_app(settings)).get("/analytics/databricks/status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["configured"])
        self.assertNotIn("private-token", response.text)

    def test_missing_connection_is_explicit(self):
        client = TestClient(create_app(Settings(_env_file=None, database_url="postgresql://placeholder")))
        self.assertFalse(client.get("/analytics/databricks/status").json()["configured"])
        self.assertEqual(client.get("/analytics/databricks/runs").status_code, 503)

    def test_all_result_chunks_are_read(self):
        warehouse = Warehouse(Settings(_env_file=None, database_url="postgresql://placeholder"))
        initial = {"manifest": {"schema": {"columns": [{"name": "value"}]}},
                   "result": {"data_array": [["first"]], "next_chunk_internal_link": "/api/2.0/sql/statements/test/result/chunks/1"}}
        with patch.object(warehouse, "call", return_value={"data_array": [["second"]]}):
            self.assertEqual(warehouse.rows(initial), [{"value": "first"}, {"value": "second"}])

    def test_truncated_results_are_not_presented_as_complete(self):
        warehouse = Warehouse(Settings(_env_file=None, database_url="postgresql://placeholder"))
        with self.assertRaises(HTTPException) as error:
            warehouse.rows({"manifest": {"truncated": True}})
        self.assertEqual(error.exception.status_code, 413)

    def test_exported_payload_is_decoded_without_changing_text(self):
        row = parse_record('{"payload":"{\\"row\\":{\\"status\\":\\"resolved\\"}}","details":"not-json","id":"abc"}')
        self.assertEqual(row["payload"]["row"]["status"], "resolved")
        self.assertEqual(row["details"], "not-json")

    def test_run_query_is_scoped_and_parameterized(self):
        run = "00000000-0000-0000-0000-000000000001"
        settings = Settings(_env_file=None, database_url="postgresql://placeholder")
        with patch.object(Warehouse, "sql", return_value=[{"dataset": "scenarios", "record": '{"id":"' + run + '"}'}]) as sql:
            response = TestClient(create_app(settings)).get("/analytics/databricks/runs/" + run)
            self.assertEqual(response.status_code, 200)
            statement, params = sql.call_args.args
            self.assertNotIn(run, statement)
            self.assertTrue(all(part.lstrip().startswith("SELECT") for part in statement.split(" UNION ALL ")))
            self.assertEqual(params[0]["value"], run)
            self.assertEqual(len(response.json()["datasets"]), 7)


if __name__ == "__main__":
    unittest.main()
