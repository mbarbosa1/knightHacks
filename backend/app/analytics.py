"""Operator analytics. All data reads use Arwa's Databricks SQL warehouse.

These routes never ingest, score, dispatch, or mutate simulation data. Keep them
out of agent tool registries: truth is an operator/evaluation-only data source.
"""
import asyncio
import json
import logging
import ssl
import time
from urllib import error, parse, request
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .config import Settings

log = logging.getLogger(__name__)
MAX_ROWS = 50000
DATASETS = {
    "scenarios": ("truth.scenarios", "id"),
    "building_damage": ("truth.truth_building_damage", "scenario_id"),
    "road_status": ("truth.truth_road_status", "scenario_id"),
    "people": ("truth.truth_people", "scenario_id"),
    "truth_links": ("truth.truth_links", "scenario_id"),
    "events": ("history.simulation_events", "run_id"),
    "scorecards": ("eval.scorecards", "scenario_id"),
}


def parse_record(row):
    """Decode known JSON columns without guessing or coercing other strings."""
    record = json.loads(row) if isinstance(row, str) else dict(row)
    for key in ("payload", "params", "details", "sampled_attributes"):
        if isinstance(record.get(key), str):
            try:
                record[key] = json.loads(record[key])
            except (TypeError, ValueError):
                pass
    return record


class Warehouse:
    def __init__(self, settings):
        self.settings = settings
        self.host = (settings.databricks_host or "").rstrip("/")
        self.warehouse = (settings.databricks_http_path or "").rstrip("/").rsplit("/", 1)[-1]
        self.configured = bool(self.host and self.warehouse and settings.databricks_token)
        self.cache = {}
        self.lock = asyncio.Lock()
        try:
            import certifi
            self.ssl = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            self.ssl = ssl.create_default_context()

    def call(self, method, path, body=None):
        if not self.configured:
            raise HTTPException(503, "Databricks is not connected to this app yet.")
        if parse.urlsplit(self.host).scheme != "https":
            raise HTTPException(503, "The Databricks workspace must use an HTTPS URL.")
        req = request.Request(self.host + path,
            json.dumps(body).encode() if body is not None else None,
            {"Authorization": "Bearer " + self.settings.databricks_token.get_secret_value(),
             "Content-Type": "application/json"}, method=method)
        try:
            with request.urlopen(req, timeout=20, context=self.ssl) as response:
                return json.load(response)
        except error.HTTPError as exc:
            message = {
                401: "Databricks authentication expired or was rejected.",
                403: "This connection does not have permission to read the Databricks data.",
                404: "The Databricks workspace, warehouse, or Genie space could not be found.",
                429: "Databricks is busy. Try again shortly.",
            }.get(exc.code, "Databricks could not complete the request.")
            raise HTTPException(502, message) from None
        except (error.URLError, TimeoutError, ValueError):
            raise HTTPException(502, "The Databricks workspace could not be reached.") from None

    def rows(self, response):
        """Read every result chunk; a partial result must never look complete."""
        if response.get("manifest", {}).get("truncated"):
            raise HTTPException(413, "This run exceeds the warehouse result size. Narrow the data before replaying it.")
        columns = [c["name"] for c in response.get("manifest", {}).get("schema", {}).get("columns", [])]
        result = response.get("result", {})
        values = list(result.get("data_array") or [])
        seen = set()
        while result.get("next_chunk_internal_link"):
            link = result["next_chunk_internal_link"]
            if not link.startswith("/api/2.0/sql/statements/") or link in seen:
                raise HTTPException(502, "Databricks returned an invalid result cursor.")
            seen.add(link)
            result = self.call("GET", link)
            values.extend(result.get("data_array") or [])
            if len(values) > MAX_ROWS:
                raise HTTPException(413, "This run is too large to replay in one browser session.")
        if len(values) > MAX_ROWS:
            raise HTTPException(413, "This run is too large to replay in one browser session.")
        return [dict(zip(columns, row)) for row in values]

    def sql(self, statement, parameters=()):
        response = self.call("POST", "/api/2.0/sql/statements", {
            "warehouse_id": self.warehouse, "statement": statement,
            "parameters": list(parameters), "wait_timeout": "10s", "on_wait_timeout": "CONTINUE"})
        deadline = time.monotonic() + 35
        while response.get("status", {}).get("state") in {"PENDING", "RUNNING"}:
            if time.monotonic() >= deadline:
                try:
                    self.call("POST", f'/api/2.0/sql/statements/{response["statement_id"]}/cancel', {})
                except HTTPException:
                    pass
                raise HTTPException(504, "The Databricks warehouse is taking longer than expected. Try again.")
            time.sleep(1)
            response = self.call("GET", f'/api/2.0/sql/statements/{response["statement_id"]}')
        if response.get("status", {}).get("state") != "SUCCEEDED":
            log.warning("Databricks analytics statement failed: %s", response.get("status", {}).get("state"))
            raise HTTPException(502, "Databricks could not read the KnightHacks tables. Check catalog and warehouse permissions.")
        return self.rows(response)

    async def cached_sql(self, key, statement, parameters=()):
        async with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 30:
                return cached[1]
            rows = await asyncio.to_thread(self.sql, statement, parameters)
            if len(self.cache) >= 12:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic(), rows)
            return rows


class GenieQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    run_id: UUID


def make_router(settings: Settings):
    router = APIRouter(prefix="/analytics/databricks", tags=["operator analytics"])
    warehouse = Warehouse(settings)

    @router.get("/status")
    async def status():
        return {"configured": warehouse.configured,
                "workspace_url": warehouse.host if warehouse.configured else None,
                "genie_configured": bool(warehouse.configured and settings.databricks_genie_space_id),
                "source": "Databricks", "catalog": "knighthacks"}

    @router.get("/runs")
    async def runs(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        rows = await warehouse.cached_sql(("runs", limit, offset), """
            SELECT to_json(struct(s.*)) AS scenario, count(*) OVER () AS total,
                coalesce(h.event_count, 0) AS event_count, coalesce(c.score_count, 0) AS score_count
            FROM knighthacks.truth.scenarios s
            LEFT JOIN (SELECT run_id, count(*) AS event_count FROM knighthacks.history.simulation_events
                       GROUP BY run_id) h ON h.run_id = s.id
            LEFT JOIN (SELECT scenario_id, count(*) AS score_count FROM knighthacks.eval.scorecards
                       GROUP BY scenario_id) c ON c.scenario_id = s.id
            ORDER BY s.started_at DESC NULLS LAST, s.id
            LIMIT :limit OFFSET :offset
        """, [{"name": "limit", "value": str(limit), "type": "INT"},
              {"name": "offset", "value": str(offset), "type": "INT"}])
        return {"runs": [{**parse_record(r["scenario"]), "event_count": int(r["event_count"]),
                          "score_count": int(r["score_count"])} for r in rows],
                "total": int(rows[0]["total"]) if rows else 0}

    @router.get("/runs/{run_id}")
    async def run_data(run_id: UUID):
        # Identifiers come exclusively from the constant allowlist. Values are parameters.
        statement = " UNION ALL ".join(
            f"SELECT '{name}' AS dataset, to_json(struct(*)) AS record FROM knighthacks.{table} WHERE {key} = :run_id"
            for name, (table, key) in DATASETS.items()) + f" LIMIT {MAX_ROWS + 1}"
        rows = await warehouse.cached_sql(("run", str(run_id)), statement,
            [{"name": "run_id", "value": str(run_id), "type": "STRING"}])
        data = {name: [] for name in DATASETS}
        for row in rows:
            data[row["dataset"]].append(parse_record(row["record"]))
        if not data["scenarios"]:
            raise HTTPException(404, "This scenario is not present in Databricks.")
        return {"run_id": str(run_id), "source": "Databricks", "datasets": data,
                "loaded_at": time.time(), "complete": True}

    @router.post("/ask")
    async def ask(body: GenieQuestion):
        if not settings.databricks_genie_space_id:
            raise HTTPException(503, "Ask Genie has not been connected for this workspace.")
        # Context narrows the question, but access is enforced by the service identity's
        # Unity Catalog/Genie permissions. Genie is an operator tool, never a dispatch tool.
        space = parse.quote(settings.databricks_genie_space_id, safe="")
        base = f"/api/2.0/genie/spaces/{space}"
        async def call(method, path, value=None):
            return await asyncio.to_thread(warehouse.call, method, path, value)
        response = await call("POST", base + "/start-conversation", {
            "content": f"Analyze KnightHacks scenario {body.run_id}. Filter all run/scenario tables to that ID. "
                       "Only read data; do not recommend or execute live dispatch actions. " + body.question})
        conversation, message = response["conversation_id"], response["message_id"]
        path = f"{base}/conversations/{conversation}/messages/{message}"
        deadline = time.monotonic() + 45
        while response.get("status") not in {"COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"}:
            if time.monotonic() > deadline:
                raise HTTPException(504, "Genie is taking longer than expected. Try again.")
            await asyncio.sleep(1)
            response = await call("GET", path)
        if response.get("status") != "COMPLETED":
            raise HTTPException(502, "Genie could not answer this question.")
        text = []
        queries = []
        for attachment in response.get("attachments", []):
            if attachment.get("text"):
                text.append(attachment["text"].get("content", ""))
            if attachment.get("query"):
                result = await call("GET", path + f'/attachments/{attachment["attachment_id"]}/query-result')
                statement = result.get("statement_response", {})
                queries.append({"description": attachment["query"].get("description", ""),
                                "sql": attachment["query"].get("query", ""),
                                "rows": await asyncio.to_thread(warehouse.rows, statement)})
        return {"text": "\n\n".join(text), "queries": queries, "source": "Databricks Genie"}

    return router
