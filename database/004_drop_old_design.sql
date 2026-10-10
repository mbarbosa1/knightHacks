-- Removes the pre-agreement "staticMapData" design (and ops) so database/schema.sql,
-- the team's agreed design (database/final-database.md), can be applied.
-- Run on the DEV branch only, as the owner, in the same transaction as
-- schema.sql. Verified empty on 2026-10-10 (0 rows in every table).
-- Destroys all tables, data, triggers and functions in both schemas.

DROP SCHEMA IF EXISTS "staticMapData" CASCADE;
DROP SCHEMA IF EXISTS ops CASCADE;
