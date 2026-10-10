-- Apply separately with a direct owner connection on the development branch.
-- Provision a LOGIN role named knighthacks_backend with a password via the
-- Neon console/secure provisioning script first. Never store passwords here.
-- It must NOT be a member of neon_superuser or another privileged role.
BEGIN;
GRANT CONNECT ON DATABASE neondb TO knighthacks_backend;
GRANT USAGE ON SCHEMA static, ops, public TO knighthacks_backend;
GRANT SELECT ON ALL TABLES IN SCHEMA static TO knighthacks_backend;
GRANT SELECT, INSERT, UPDATE ON ops.simulation_runs, ops.observations TO knighthacks_backend;
GRANT SELECT ON ops.incidents, ops.resources TO knighthacks_backend;
-- Lets the backend append to the outbox directly if it ever needs to.
GRANT INSERT ON ops.operational_events TO knighthacks_backend;
COMMIT;
