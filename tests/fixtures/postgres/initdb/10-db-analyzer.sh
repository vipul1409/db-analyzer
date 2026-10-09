#!/bin/sh
# Fixture setup: extensions an admin would install, a schema the analyzer role cannot read,
# a writable role, then the documented read-only role script (password = role name, fixtures only).
set -e
psql -v ON_ERROR_STOP=1 -U postgres -d app <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pgstattuple;
CREATE EXTENSION IF NOT EXISTS hypopg;
CREATE SCHEMA private;
CREATE TABLE private.notes (id int PRIMARY KEY, body text);
-- A misconfigured analyzer login: not superuser, but writable. Must be refused.
CREATE ROLE db_writer LOGIN PASSWORD 'db_writer';
GRANT CONNECT ON DATABASE app TO db_writer;
-- Looks read-only (read-only default) but holds a write grant. Must be refused.
CREATE ROLE db_sneaky LOGIN PASSWORD 'db_sneaky';
ALTER ROLE db_sneaky SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE app TO db_sneaky;
GRANT USAGE ON SCHEMA private TO db_sneaky;
GRANT SELECT, INSERT ON private.notes TO db_sneaky;
SQL
psql -v ON_ERROR_STOP=1 -U postgres -d app -v password=db_analyzer -v dbname=app \
  -f /setup/postgres_role.sql
