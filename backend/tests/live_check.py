"""Exercise the API with the configured simulator login; roll back ALL fixtures.

Run from backend: python -B tests/live_check.py --env-file .env
Uses an in-process ASGI client and real Postgres transactions. No extra packages.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg.rows import dict_row

from app.config import Settings
from app.main import create_app


class TransactionPool:
    def __init__(self, connection):
        self.conn = connection

    async def open(self):
        pass

    async def close(self):
        pass

    @asynccontextmanager
    async def connection(self):
        async with self.conn.transaction():
            yield self.conn


async def request(app, method, path, body=None, expected=200, query=""):
    messages = []
    encoded = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": query.encode(), "root_path": "",
             "headers": [(b"content-type", b"application/json")],
             "client": ("127.0.0.1",1234), "server": ("127.0.0.1",8000)}
    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    data = json.loads(b"".join(m.get("body",b"") for m in messages if m["type"] == "http.response.body"))
    assert status == expected, (method,path,status,data)
    return data


async def main(env_file):
    settings = Settings(_env_file=env_file)
    conn = await psycopg.AsyncConnection.connect(settings.database_url.get_secret_value(),
                                                row_factory=dict_row, prepare_threshold=None, connect_timeout=10)
    try:
        # Start an outer transaction. Each API request uses a nested savepoint;
        # nothing is committed, even if an assertion or a request fails.
        role = await (await conn.execute("SELECT current_user AS role")).fetchone()
        assert role["role"] == "simulator", "Use the simulator login, never the owner"
        app = create_app(settings, TransactionPool(conn))
        async with app.router.lifespan_context(app):
            await request(app,"GET","/health/ready")
            versions = await request(app,"GET","/map/versions")
            assert versions, "Import a real map before running this check"
            version = versions[0]["id"]
            buildings = await request(app,"GET",f"/map/versions/{version}/buildings")
            roads = await request(app,"GET",f"/map/versions/{version}/roads")
            assert buildings["type"] == roads["type"] == "FeatureCollection"
            counts = await (await conn.execute(
                "SELECT (SELECT count(*) FROM static.buildings WHERE map_version_id=%s) AS buildings, "
                "(SELECT count(*) FROM static.road_segments WHERE map_version_id=%s) AS roads", (version,version)
            )).fetchone()
            assert len(buildings["features"]) == counts["buildings"]
            assert len(roads["features"]) == counts["roads"]
            assert "height_m" in buildings["features"][0]["properties"]
            assert isinstance(roads["features"][0]["properties"]["from_node"], str)
            await request(app,"GET",f"/map/versions/{version}/buildings",query="bbox=1,0,0,1",expected=422)
            empty = await request(app,"GET",f"/map/versions/{version}/buildings",query="bbox=0,0,1,1")
            assert empty["features"] == []
            await request(app,"GET","/map/versions/nonexistent/buildings",expected=404)
            facilities = await request(app,"GET",f"/map/versions/{version}/facilities")
            await request(app,"GET",f"/map/versions/{version}/buildings/{buildings['features'][0]['id']}")
            run_body = {"map_version_id":version,"seed":42,"hazard_types":["earthquake"]}
            run = await request(app,"POST","/runs",run_body,expected=201)
            other = await request(app,"POST","/runs",run_body,expected=201)
            assert "seed" not in run
            base = f"/runs/{run['id']}"
            obs = {"id":str(uuid4()),"channel":"sensor","format":"text",
                   "original_text":"TEST FIXTURE: smoke","source_reliability":0.7,"sim_time_s":0.1}
            saved = await request(app,"POST",base+"/observations",obs)
            await request(app,"POST",base+"/observations",obs)
            await request(app,"POST",base+"/observations",{**obs,"sim_time_s":2},expected=409)
            await request(app,"POST",base+"/observations",{**obs,"media_url":"https://example.org/changed"},expected=409)
            resource_id = str(uuid4())
            resource = {"type":"ambulance","capacity":4,"status":"available",
                        "home_facility_id":facilities[0]["id"],"location":{"longitude":139.7,"latitude":35.7}}
            saved_resource = await request(app,"PUT",base+f"/resources/{resource_id}",resource)
            await request(app,"PUT",base+f"/resources/{resource_id}",resource)
            await request(app,"PUT",base+f"/resources/{resource_id}",{**resource,"status":"en_route"})
            await request(app,"PUT",f"/runs/{other['id']}/resources/{resource_id}",resource,expected=409)
            await request(app,"PUT",base+f"/resources/{uuid4()}",{**resource,"home_facility_id":"missing"},expected=422)
            incident_id = str(uuid4())
            incident = {"building_id":buildings["features"][0]["id"],"status":"reported",
                        "evidence":{"supports":[saved["id"]]},
                        "person_groups":[{"group_id":"group-1","est_count":3}]}
            saved_incident = await request(app,"PUT",base+f"/incidents/{incident_id}",incident)
            retry = await request(app,"PUT",base+f"/incidents/{incident_id}",incident)
            assert saved_incident["updated_at"] == retry["updated_at"]
            await request(app,"PUT",base+f"/incidents/{incident_id}",{**incident,"status":"corroborated"})
            await request(app,"PUT",f"/runs/{other['id']}/incidents/{uuid4()}",incident,expected=422)
            await request(app,"PUT",base+f"/incidents/{uuid4()}",{**incident,"building_id":"missing"},expected=422)
            road_id = roads["features"][0]["id"]
            restriction = {"status":"BLOCKED","source_observation_id":saved["id"]}
            saved_road = await request(app,"PUT",base+f"/road-restrictions/{road_id}",restriction)
            retry = await request(app,"PUT",base+f"/road-restrictions/{road_id}",restriction)
            assert saved_road["updated_at"] == retry["updated_at"]
            await request(app,"PUT",base+f"/road-restrictions/{road_id}",{**restriction,"status":"OPEN"})
            await request(app,"PUT",f"/runs/{other['id']}/road-restrictions/{road_id}",restriction,expected=422)
            assert len(await request(app,"GET",base+"/resources")) == 1
            assert len(await request(app,"GET",base+"/incidents")) == 1
            assert len(await request(app,"GET",base+"/road-restrictions")) == 1
            ended = await request(app,"POST",base+"/end",{"status":"completed","sim_time_s":60})
            retry = await request(app,"POST",base+"/end",{"status":"completed","sim_time_s":60})
            assert ended["ended_at"] == retry["ended_at"]
            await request(app,"POST",base+"/end",{"status":"failed"},expected=409)
            for suffix,body in ((f"/resources/{resource_id}",resource),
                                (f"/incidents/{incident_id}",incident),
                                (f"/road-restrictions/{road_id}",restriction)):
                await request(app,"PUT",base+suffix,body,expected=409)
            await request(app,"POST",base+"/observations",{**obs,"id":str(uuid4())},expected=409)
            await request(app,"POST",base+"/observations",obs)
            # Initial ingestion is atomic and repeatable, with a public readiness receipt.
            stable_id = str(uuid4())
            stable_body = {**run_body,"id":stable_id}
            snapshot_run = await request(app,"POST","/runs",stable_body,expected=201)
            assert (await request(app,"POST","/runs",stable_body,expected=201))["id"] == stable_id
            snapshot_base = f"/runs/{stable_id}"
            snapshot_obs = {**obs,"id":str(uuid4())}
            snapshot_resource = {**resource,"id":str(uuid4())}
            invalid = {"observations":[snapshot_obs],"resources":[{**snapshot_resource,"home_facility_id":"missing"}]}
            await request(app,"POST",snapshot_base+"/ingest",invalid,expected=422)
            assert (await request(app,"GET",snapshot_base+"/ingestion"))["ready"] is False
            assert await request(app,"GET",snapshot_base+"/observations") == []
            snapshot = {"observations":[snapshot_obs],"resources":[snapshot_resource]}
            receipt = await request(app,"POST",snapshot_base+"/ingest",snapshot)
            assert receipt["ready"] and receipt["stored_observations"] == receipt["stored_resources"] == 1
            await request(app,"POST",snapshot_base+"/ingest",snapshot)
            await request(app,"PUT",snapshot_base+f"/resources/{snapshot_resource['id']}",{**resource,"status":"busy"})
            await request(app,"POST",snapshot_base+"/ingest",snapshot)
            assert (await request(app,"GET",snapshot_base+"/resources"))[0]["status"] == "busy"
            await request(app,"POST",snapshot_base+"/ingest",{"observations":[{**snapshot_obs,"original_text":"changed"}],"resources":[snapshot_resource]},expected=409)
            feed = await request(app,"GET",snapshot_base+"/observations",query="limit=1")
            assert feed[0]["id"] == snapshot_obs["id"] and "answer" not in feed[0]
            assert await request(app,"GET",snapshot_base+"/observations",query=f"after={feed[0]['id']}") == []
            await request(app,"GET",snapshot_base+"/observations",query=f"after={saved['id']}",expected=404)
            events = await (await conn.execute(
                "SELECT table_name,count(*) AS count FROM ops.operational_events WHERE run_id=%s GROUP BY table_name", (run["id"],)
            )).fetchall()
            assert {r["table_name"]:r["count"] for r in events} == {
                "simulation_runs":3,"observations":1,"resources":2,"incidents":2,"road_restrictions":2}
            print(f"PASS: API flow as simulator; {counts['buildings']} buildings, {counts['roads']} roads; retries, run isolation, terminal states, event history")
    finally:
        await conn.rollback()
        await conn.close()
        print("Test transaction rolled back; no fixture records retained")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    asyncio.run(main(args.env_file))
