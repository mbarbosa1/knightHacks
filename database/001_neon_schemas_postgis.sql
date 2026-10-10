-- KnightHacks / main / neondb
-- Project: soft-mountain-91298548
-- Branch: br-quiet-grass-b4ws3sil
-- Preserves the existing case-sensitive staticMapData schema.
-- Run in Neon SQL Editor using the database owner role.
'''
RECORD - DONT RUNNNNN PLS
Turned on PostGIS (map/geometry support) and created the staticMapData and ops schemas.
'''
BEGIN;

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS "staticMapData";
CREATE SCHEMA IF NOT EXISTS ops;

COMMIT;

-- Verification: both schemas should appear.
SELECT current_database() AS database_name,
       nspname AS schema_name
FROM pg_namespace
WHERE nspname IN ('staticMapData', 'ops')
ORDER BY nspname;

-- Verification: confirms the installed extension and its schema.
SELECT e.extname, e.extversion, n.nspname AS extension_schema
FROM pg_extension AS e
JOIN pg_namespace AS n ON n.oid = e.extnamespace
WHERE e.extname = 'postgis';

-- Verification: exercises PostGIS without assuming its extension schema.
DO $verification$
DECLARE
    extension_schema text;
    point_srid integer;
BEGIN
    SELECT n.nspname INTO STRICT extension_schema
    FROM pg_extension AS e
    JOIN pg_namespace AS n ON n.oid = e.extnamespace
    WHERE e.extname = 'postgis';

    EXECUTE format(
        'SELECT %1$I.ST_SRID(%1$I.ST_SetSRID(%1$I.ST_MakePoint(139.7, 35.69), 4326))',
        extension_schema
    ) INTO point_srid;

    IF point_srid IS DISTINCT FROM 4326 THEN
        RAISE EXCEPTION 'PostGIS verification failed: unexpected SRID %', point_srid;
    END IF;

    RAISE NOTICE 'PostGIS spatial check passed: SRID %', point_srid;
END;
$verification$;
