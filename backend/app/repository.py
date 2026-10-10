from fastapi import HTTPException

from .models import ObservationCreate, RunCreate

# Tables follow database/schema.sql (docs: database/final-database.md).


async def require_map(connection, version_id):
    row = await (await connection.execute(
        "SELECT id, created_at, source_versions, notes FROM static.map_versions WHERE id=%s",
        (version_id,),
    )).fetchone()
    if row is None:
        raise HTTPException(404, "Map version not found")
    return row


async def require_run(connection, run_id, lock=False):
    query = "SELECT id, map_version_id, hazard_types, status, started_at, ended_at FROM ops.simulation_runs WHERE id=%s"
    if lock:
        query += " FOR UPDATE"
    row = await (await connection.execute(query, (run_id,))).fetchone()
    if row is None:
        raise HTTPException(404, "Run not found")
    return row


async def list_facilities(connection, version_id):
    await require_map(connection, version_id)
    return await (await connection.execute(
        '''SELECT id, type, subtype, name_en, name_ja, building_id, capacity, access_node,
                  public.ST_AsGeoJSON(geom)::jsonb AS geometry
           FROM static.facilities WHERE map_version_id=%s ORDER BY type, id''',
        (version_id,),
    )).fetchall()


async def get_building_context(connection, version_id, building_id, radius_m, limit):
    await require_map(connection, version_id)
    building = await (await connection.execute(
        '''SELECT id, map_version_id, map_id, height_m, floors, basement_floors, footprint_m2,
                  structure, fire_resistance, fireproof_code, year_built, era, era_probs,
                  usage, soil_amp, facility_type, facility_id, name_en, name_ja,
                  address_en, address_ja,
                  public.ST_AsGeoJSON(geom)::jsonb AS geometry
           FROM static.buildings WHERE map_version_id=%s AND id=%s''',
        (version_id, building_id),
    )).fetchone()
    if building is None:
        raise HTTPException(404, "Building not found")
    facilities = await (await connection.execute(
        '''SELECT f.id, f.type, f.subtype, f.name_en, f.name_ja, f.building_id, f.capacity,
                  f.access_node, public.ST_AsGeoJSON(f.geom)::jsonb AS geometry,
                  public.ST_Distance(f.geom::public.geography,
                      public.ST_PointOnSurface(b.geom)::public.geography) AS distance_m
           FROM static.facilities f
           JOIN static.buildings b ON b.map_version_id=f.map_version_id
           WHERE b.map_version_id=%s AND b.id=%s
             AND public.ST_DWithin(f.geom::public.geography,
                 public.ST_PointOnSurface(b.geom)::public.geography, %s)
           ORDER BY distance_m, f.id LIMIT %s''',
        (version_id, building_id, radius_m, limit),
    )).fetchall()
    return {"building": building, "nearby_facilities": facilities}


async def create_run(connection, body: RunCreate):
    await require_map(connection, body.map_version_id)
    # The seed is deliberately absent from the returned run context.
    return await (await connection.execute(
        '''INSERT INTO ops.simulation_runs (map_version_id, seed, hazard_types, status, started_at)
           VALUES (%s,%s,%s,'created',now())
           RETURNING id, map_version_id, hazard_types, status, started_at, ended_at''',
        (body.map_version_id, body.seed, body.hazard_types),
    )).fetchone()


async def get_observation(connection, run_id, observation_id):
    return await (await connection.execute(
        '''SELECT id, run_id, channel, format, original_text, lang, text_en,
                  media_url, claimed_location, location_precision, source_reliability,
                  received_at, sim_time_s,
                  public.ST_X(claimed_geom) AS longitude, public.ST_Y(claimed_geom) AS latitude
           FROM ops.observations WHERE run_id=%s AND id=%s''',
        (run_id, observation_id),
    )).fetchone()


async def save_observation(connection, run_id, body: ObservationCreate):
    run = await require_run(connection, run_id, lock=True)
    if body.id is not None:
        existing = await get_observation(connection, run_id, body.id)
        if existing:
            # A retry with the same ID returns the stored row instead of a duplicate.
            if any(existing[key] != getattr(body, key)
                   for key in ("channel", "format", "original_text", "lang", "text_en")):
                raise HTTPException(409, "Observation ID already exists with a different payload")
            return existing
    if run["status"] in {"completed", "failed"} or run["ended_at"] is not None:
        raise HTTPException(409, "Run has ended")
    location = body.location
    row = await (await connection.execute(
        '''INSERT INTO ops.observations
           (id,run_id,channel,format,original_text,lang,text_en,media_url,
            claimed_location,claimed_geom,location_precision,source_reliability,sim_time_s)
           VALUES (COALESCE(%s, gen_random_uuid()),%s,%s::ops.observation_channel,%s,%s,%s,%s,%s,%s,
             CASE WHEN %s::double precision IS NULL THEN NULL ELSE
               public.ST_SetSRID(public.ST_MakePoint(%s,%s),4326) END,%s,%s,%s)
           RETURNING id''',
        (body.id, run_id, body.channel, body.format, body.original_text, body.lang,
         body.text_en, body.media_url, body.claimed_location,
         location.longitude if location else None,
         location.longitude if location else None, location.latitude if location else None,
         body.location_precision, body.source_reliability, body.sim_time_s),
    )).fetchone()
    await connection.execute(
        "UPDATE ops.simulation_runs SET status='running' WHERE id=%s AND status='created'",
        (run_id,),
    )
    return await get_observation(connection, run_id, row["id"])
