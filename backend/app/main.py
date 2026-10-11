from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from psycopg_pool import PoolTimeout

from . import repository
from .config import Settings
from .db import get_connection, make_pool
from .models import IncidentPut, ObservationCreate, ResourcePut, RoadRestrictionPut, RunCreate, RunEnd, SimulationIngest


def create_app(settings: Settings | None = None, pool=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings()
        app.state.pool = pool if pool is not None else make_pool(config)
        try:
            await app.state.pool.open()
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(title="KnightHacks backend", version="0.1.0", lifespan=lifespan)
    # Environment configuration is also used when constructing the app, so CORS
    # works consistently for the browser and keeps its list explicit.
    config = settings or Settings()
    app.add_middleware(CORSMiddleware, allow_origins=config.cors_origins,
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type"])
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    async def unavailable(_request, _exc):
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})

    for error in (OperationalError, PoolTimeout):
        app.add_exception_handler(error, unavailable)

    async def conflict(_request, _exc):
        return JSONResponse(status_code=409, content={"detail": "Database reference or constraint conflict"})

    for error in (ForeignKeyViolation, UniqueViolation, CheckViolation):
        app.add_exception_handler(error, conflict)

    Connection = Annotated[object, Depends(get_connection, scope="function")]

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(connection: Connection):
        await connection.execute("SELECT id FROM static.map_versions LIMIT 1")
        await connection.execute("SELECT id FROM ops.simulation_runs LIMIT 1")
        return {"status": "ok", "database": "connected"}

    @app.get("/map/versions")
    async def versions(connection: Connection, limit: int = Query(100, ge=1, le=1000)):
        return await (await connection.execute(
            'SELECT id, created_at, source_versions, notes FROM static.map_versions ORDER BY created_at DESC, id LIMIT %s',
            (limit,),
        )).fetchall()

    @app.get("/map/versions/{version_id}")
    async def version(version_id: str, connection: Connection):
        return await repository.require_map(connection, version_id)

    @app.get("/map/versions/{version_id}/facilities")
    async def facilities(version_id: str, connection: Connection):
        return await repository.list_facilities(connection, version_id)

    @app.get("/map/versions/{version_id}/buildings")
    async def buildings(version_id: str, connection: Connection, bbox: str | None = None):
        return await repository.bulk_map(connection, version_id, "buildings", repository.parse_bbox(bbox))

    @app.get("/map/versions/{version_id}/roads")
    async def roads(version_id: str, connection: Connection, bbox: str | None = None):
        return await repository.bulk_map(connection, version_id, "road_segments", repository.parse_bbox(bbox))

    @app.get("/map/versions/{version_id}/buildings/{building_id}")
    async def building(version_id: str, building_id: str, connection: Connection,
                       radius_m: float = Query(1000, ge=0, le=10000, allow_inf_nan=False),
                       limit: int = Query(50, ge=1, le=500)):
        return await repository.get_building_context(connection, version_id, building_id, radius_m, limit)

    @app.post("/runs", status_code=201)
    async def run(body: RunCreate, connection: Connection):
        return await repository.create_run(connection, body)

    @app.get("/runs/{run_id}")
    async def read_run(run_id: UUID, connection: Connection):
        return await repository.require_run(connection, run_id)

    @app.post("/runs/{run_id}/end")
    async def end(run_id: UUID, body: RunEnd, connection: Connection):
        return await repository.end_run(connection, run_id, body)

    @app.put("/runs/{run_id}/resources/{resource_id}")
    async def resource(run_id: UUID, resource_id: UUID, body: ResourcePut, connection: Connection):
        return await repository.put_resource(connection, run_id, resource_id, body)

    @app.put("/runs/{run_id}/incidents/{incident_id}")
    async def incident(run_id: UUID, incident_id: UUID, body: IncidentPut, connection: Connection):
        return await repository.put_incident(connection, run_id, incident_id, body)

    @app.put("/runs/{run_id}/road-restrictions/{road_id}")
    async def road_restriction(run_id: UUID, road_id: str, body: RoadRestrictionPut, connection: Connection):
        return await repository.put_road_restriction(connection, run_id, road_id, body)

    @app.get("/runs/{run_id}/road-restrictions")
    async def road_restrictions(run_id: UUID, connection: Connection,
                                limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        await repository.require_run(connection, run_id)
        return await (await connection.execute(
            "SELECT run_id,road_id,status,source_observation_id,updated_at FROM ops.road_restrictions WHERE run_id=%s ORDER BY road_id LIMIT %s OFFSET %s",
            (run_id, limit, offset),
        )).fetchall()

    @app.post("/runs/{run_id}/observations")
    async def observation(run_id: UUID, body: ObservationCreate, connection: Connection):
        return await repository.save_observation(connection, run_id, body)

    @app.post("/runs/{run_id}/ingest")
    async def ingest(run_id: UUID, body: SimulationIngest, connection: Connection):
        return await repository.ingest_simulation(connection, run_id, body)

    @app.get("/runs/{run_id}/ingestion")
    async def ingestion(run_id: UUID, connection: Connection):
        return await repository.ingestion_status(connection, run_id)

    @app.get("/runs/{run_id}/observations")
    async def observation_feed(run_id: UUID, connection: Connection,
                               limit: int = Query(100, ge=1, le=1000), after: UUID | None = None):
        return await repository.list_observations(connection, run_id, limit, after)

    @app.get("/runs/{run_id}/observations/{observation_id}")
    async def read_observation(run_id: UUID, observation_id: UUID, connection: Connection):
        await repository.require_run(connection, run_id)
        result = await repository.get_observation(connection, run_id, observation_id)
        if result is None:
            raise HTTPException(404, "Observation not found")
        return result

    @app.get("/runs/{run_id}/incidents")
    async def incidents(run_id: UUID, connection: Connection,
                        limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        await repository.require_run(connection, run_id)
        return await (await connection.execute(
            '''SELECT id, run_id, building_id, status, person_groups, evidence,
                      uncertainty_m, created_at, updated_at,
                      public.ST_AsGeoJSON(geom)::jsonb AS location
               FROM ops.incidents WHERE run_id=%s ORDER BY created_at, id LIMIT %s OFFSET %s''',
            (run_id, limit, offset),
        )).fetchall()

    @app.get("/runs/{run_id}/resources")
    async def resources(run_id: UUID, connection: Connection,
                        limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        await repository.require_run(connection, run_id)
        return await (await connection.execute(
            '''SELECT id, run_id, type, name, status, capacity, skills, home_facility_id,
                      public.ST_AsGeoJSON(geom)::jsonb AS location
               FROM ops.resources WHERE run_id=%s ORDER BY id LIMIT %s OFFSET %s''',
            (run_id, limit, offset),
        )).fetchall()

    return app
