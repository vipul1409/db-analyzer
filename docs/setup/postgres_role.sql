-- Read-only login for DB Analyzer. Run as an admin user against the target database:
--
--   psql -v password='<secret>' -v dbname=app -f postgres_role.sql
--
-- The analyzer never writes; this role makes that true at the database layer as well.

CREATE ROLE db_analyzer LOGIN PASSWORD :'password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE db_analyzer SET default_transaction_read_only = on;
ALTER ROLE db_analyzer SET statement_timeout = '30s';
ALTER ROLE db_analyzer SET lock_timeout = '1s';
ALTER ROLE db_analyzer SET idle_in_transaction_session_timeout = '60s';

-- pg_stat_* views, pg_stat_statements text, settings, pgstattuple_approx.
GRANT pg_monitor TO db_analyzer;
GRANT CONNECT ON DATABASE :"dbname" TO db_analyzer;

-- Repeat for each schema to analyse. SELECT on tables is optional: without it inventory,
-- workload and index advice still work; exact row counts and hotspot counts are reported as
-- skipped. pgstattuple_approx needs USAGE on the schema and pg_monitor, not SELECT.
GRANT USAGE ON SCHEMA public TO db_analyzer;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO db_analyzer;
-- Tables created later need this too. Run it as each role that creates tables (e.g. the app's
-- migration role), or those tables will show as "without SELECT" in the probe:
--   ALTER DEFAULT PRIVILEGES FOR ROLE <app_owner> IN SCHEMA public
--     GRANT SELECT ON TABLES TO db_analyzer;
