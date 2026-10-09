"""Forbidden-statement corpus: SQL the analyzer must never run.

Every statement is rejected by QueryGuard under both profiles (test_guard_corpus.py). Each one
also records which layer stops it if the guard is bypassed (test_corpus_against_database.py):

- DATABASE: the read-only role and the read-only, rolled-back transaction refuse it too.
- GUARD: only the guard stops it. These succeed under any read-only role (sleeping, advisory
  locks, signalling backends, session settings, reading files or whole tables through
  functions), which is why the guard exists. The database suite does not run them.
"""

from dataclasses import dataclass
from typing import Literal

Layer = Literal["database", "guard"]


@dataclass(frozen=True)
class Forbidden:
    sql: str
    layer: Layer


def _db(*sqls: str) -> list[Forbidden]:
    return [Forbidden(s, "database") for s in sqls]


def _guard(*sqls: str) -> list[Forbidden]:
    return [Forbidden(s, "guard") for s in sqls]


DML = _db(
    "INSERT INTO tenants VALUES (999, 'x', 'free')",
    "INSERT INTO tenants SELECT * FROM tenants",
    "INSERT INTO tenants VALUES (999, 'x', 'free') ON CONFLICT DO NOTHING",
    "INSERT INTO tenants VALUES (1, 'x', 'free') ON CONFLICT (id) DO UPDATE SET name = 'y'",
    "INSERT INTO tenants VALUES (999, 'x', 'free') RETURNING *",
    "UPDATE tenants SET name = 'x'",
    "UPDATE tenants SET name = 'x' WHERE id = 1 RETURNING id",
    "UPDATE bookings b SET amount = 0 FROM accounts a WHERE a.id = b.account_id",
    "DELETE FROM audit_log",
    "DELETE FROM audit_log WHERE id = 1 RETURNING *",
    "DELETE FROM bookings USING accounts WHERE accounts.id = bookings.account_id",
    "DELETE FROM ONLY usage_records",
    "TRUNCATE audit_log",
    "TRUNCATE TABLE audit_log, legacy_imports CASCADE",
    "MERGE INTO tenants t USING (SELECT 1 AS id) s ON t.id = s.id "
    "WHEN MATCHED THEN UPDATE SET name = 'x'",
    "MERGE INTO tenants t USING (SELECT 999 AS id) s ON t.id = s.id "
    "WHEN NOT MATCHED THEN INSERT VALUES (999, 'x', 'free')",
    "UPDATE pg_catalog.pg_class SET relname = 'x' WHERE false",
    "DELETE FROM pg_catalog.pg_statistic",
    "INSERT INTO usage_records_t1 VALUES (1, 999999, 'x', 1, now())",
    "UPDATE usage_records SET quantity = 0",
)

WRITING_SELECTS = _db(
    "WITH d AS (DELETE FROM audit_log RETURNING *) SELECT count(*) FROM d",
    "WITH u AS (UPDATE tenants SET name = 'x' RETURNING id) SELECT * FROM u",
    "WITH i AS (INSERT INTO tenants VALUES (999, 'x', 'free') RETURNING id) SELECT 1",
    "WITH a AS (SELECT 1), d AS (DELETE FROM audit_log) SELECT * FROM a",
    "SELECT * FROM (WITH d AS (DELETE FROM audit_log RETURNING id) SELECT id FROM d) s",
    "SELECT * INTO new_tenants FROM tenants",
    "SELECT * INTO TEMP new_tenants FROM tenants",
    "SELECT * INTO UNLOGGED new_tenants FROM tenants",
    "CREATE TABLE new_tenants AS SELECT * FROM tenants",
    "CREATE TEMP TABLE t AS SELECT 1",
    "SELECT * FROM tenants FOR UPDATE",
    "SELECT * FROM tenants FOR NO KEY UPDATE",
    "SELECT * FROM tenants FOR SHARE",
    "SELECT * FROM tenants FOR KEY SHARE",
    "SELECT * FROM tenants FOR UPDATE NOWAIT",
    "SELECT * FROM tenants FOR UPDATE SKIP LOCKED",
    "SELECT * FROM (SELECT * FROM tenants FOR UPDATE) t",
    "SELECT 1 UNION SELECT id FROM (SELECT id FROM tenants FOR SHARE) t",
    "WITH t AS (SELECT * FROM tenants FOR UPDATE) SELECT * FROM t",
    "SELECT * FROM tenants WHERE id IN (SELECT id FROM tenants FOR UPDATE)",
)

DDL = _db(
    "CREATE TABLE t (x int)",
    "CREATE TEMP TABLE t (x int)",
    "CREATE UNLOGGED TABLE t (x int)",
    "CREATE INDEX ON events (kind)",
    "CREATE INDEX CONCURRENTLY idx_x ON events (kind)",
    "CREATE UNIQUE INDEX idx_x ON tenants (name)",
    "CREATE VIEW v AS SELECT 1",
    "CREATE OR REPLACE VIEW v AS SELECT 1",
    "CREATE MATERIALIZED VIEW mv AS SELECT 1",
    "CREATE SEQUENCE s",
    "CREATE SCHEMA s",
    "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    "CREATE OR REPLACE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$BEGIN RETURN 1; END$$",
    "CREATE PROCEDURE p() LANGUAGE sql AS 'SELECT 1'",
    "CREATE TRIGGER trg BEFORE INSERT ON tenants FOR EACH ROW EXECUTE FUNCTION f()",
    "CREATE RULE r AS ON INSERT TO tenants DO INSTEAD NOTHING",
    "CREATE POLICY p ON tenants USING (true)",
    "CREATE TYPE mood AS ENUM ('ok')",
    "CREATE DOMAIN d AS int",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE ROLE intruder",
    "CREATE USER intruder PASSWORD 'x'",
    "CREATE DATABASE other",
    "CREATE PUBLICATION pub FOR ALL TABLES",
    "CREATE SERVER s FOREIGN DATA WRAPPER postgres_fdw",
    "ALTER TABLE tenants ADD COLUMN x int",
    "ALTER TABLE tenants RENAME TO t2",
    "ALTER TABLE audit_log SET (autovacuum_enabled = true)",
    "ALTER INDEX idx_accounts_tenant RENAME TO x",
    "ALTER ROLE db_analyzer SUPERUSER",
    "ALTER ROLE db_analyzer SET default_transaction_read_only = off",
    "ALTER USER db_analyzer WITH PASSWORD 'x'",
    "ALTER DATABASE shop SET default_transaction_read_only = off",
    "ALTER SYSTEM SET work_mem = '1GB'",
    "ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO PUBLIC",
    "ALTER EXTENSION hypopg UPDATE",
    "DROP TABLE audit_log",
    "DROP TABLE IF EXISTS audit_log CASCADE",
    "DROP INDEX idx_accounts_tenant_dup",
    "DROP SCHEMA public CASCADE",
    "DROP EXTENSION hypopg",
    "DROP ROLE db_writer",
    "DROP DATABASE shop",
    "DROP OWNED BY db_analyzer",
    "COMMENT ON TABLE tenants IS 'x'",
    "GRANT ALL ON tenants TO db_analyzer",
    "GRANT pg_write_all_data TO db_analyzer",
    "REVOKE SELECT ON tenants FROM db_analyzer",
    "SECURITY LABEL ON TABLE tenants IS 'x'",
    "REFRESH MATERIALIZED VIEW mv",
    "REINDEX TABLE tenants",
    "CLUSTER tenants USING tenants_pkey",
    "VACUUM tenants",
    "VACUUM FULL tenants",
    "CHECKPOINT",
    "LOCK TABLE tenants IN ACCESS EXCLUSIVE MODE",
    "LOCK TABLE tenants IN SHARE MODE",
    "IMPORT FOREIGN SCHEMA s FROM SERVER srv INTO public",
    "REASSIGN OWNED BY db_analyzer TO postgres",
)

SESSION_AND_TRANSACTION = _db(
    "SET ROLE postgres",
    "SET SESSION AUTHORIZATION postgres",
    "COMMIT PREPARED 'x'",
    "DISCARD ALL",
) + _guard(
    "SET default_transaction_read_only = off",
    "SET statement_timeout = 0",
    "SET LOCAL statement_timeout = 0",
    "SET LOCAL work_mem = '4GB'",
    "SET search_path = evil, public",
    "SET work_mem TO '4GB'",
    "RESET statement_timeout",
    "RESET ALL",
    "ROLLBACK",
    "SAVEPOINT s",
    "UNLISTEN *",
    "SET LOCAL lock_timeout = 0",
    "SET plan_cache_mode = force_generic_plan",
    "LOCK TABLE tenants IN ACCESS SHARE MODE",
)

PROCEDURAL = _db(
    "DO $$BEGIN DELETE FROM audit_log; END$$",
    "EXPLAIN DELETE FROM audit_log",
    "EXPLAIN UPDATE bookings SET amount = amount WHERE tenant_id = 7",
    "EXPLAIN INSERT INTO tenants VALUES (999, 'x', 'free')",
    "EXPLAIN (FORMAT JSON) UPDATE bookings SET amount = amount WHERE tenant_id = 7",
    "EXPLAIN ANALYZE DELETE FROM audit_log",
    "EXPLAIN SELECT * FROM tenants FOR UPDATE",
    "COPY tenants FROM STDIN",
    "COPY tenants FROM '/tmp/x.csv'",
    "COPY tenants TO '/tmp/x.csv'",
    "COPY (SELECT * FROM accounts) TO '/tmp/x.csv'",
    "COPY tenants TO PROGRAM 'cat > /dev/null'",
    "LOAD 'plpgsql'",
) + _guard(
    "EXPLAIN ANALYZE SELECT * FROM events",
    "EXPLAIN (ANALYZE) SELECT 1",
    "EXPLAIN (ANALYZE true, FORMAT JSON) SELECT 1",
    "EXPLAIN (ANALYZE false) SELECT 1",
    "EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM events",
    "EXPLAIN ANALYZE SELECT pg_sleep(1)",
    "EXPLAIN SELECT pg_sleep(1)",
    "COPY tenants TO STDOUT",
    "COPY (SELECT email FROM accounts) TO STDOUT",
)

MULTI_AND_MALFORMED = _db(
    "SELECT 1; DELETE FROM audit_log",
    "SELECT 1; DROP TABLE audit_log",
    "SELECT 1;; DELETE FROM audit_log",
    "SELECT 1; COMMIT; DELETE FROM audit_log",
    "SELECT 1 /* ; */; DELETE FROM audit_log",
    "SELECT $$;$$; DELETE FROM audit_log",
    "BEGIN; DELETE FROM audit_log; COMMIT",
    "not sql at all",
    "SELEC 1",
    "SELECT * FROM",
    "DELETE",
    "SELECT 'unterminated",
) + _guard(
    "",
    ";",
    "   ",
    "-- only a comment",
    "SELECT 1; SELECT 2",
)

# Functions with side effects, or that read files, run SQL text, or read whole tables
# (bypassing the gate and the privacy filter). Each is also tried in other positions below.
_FUNCTIONS = [
    "pg_sleep(0.01)",
    "pg_sleep_for('10 milliseconds')",
    "pg_sleep_until(now())",
    "pg_advisory_lock(1)",
    "pg_advisory_lock_shared(1)",
    "pg_try_advisory_lock(1)",
    "pg_try_advisory_lock_shared(1)",
    "pg_advisory_xact_lock(1)",
    "pg_try_advisory_xact_lock(1)",
    "pg_advisory_unlock_all()",
    "pg_cancel_backend(pg_backend_pid())",
    "set_config('statement_timeout', '0', false)",
    "set_config('default_transaction_read_only', 'off', false)",
    "pg_catalog.set_config('search_path', 'evil', true)",
    "query_to_xml('SELECT * FROM accounts', true, false, '')",
    "table_to_xml('accounts', true, false, '')",
    "schema_to_xml('public', true, false, '')",
    "database_to_xml(true, false, '')",
    "ts_stat('SELECT to_tsvector(email) FROM accounts')",
    "pg_stat_clear_snapshot()",
    "currval('x')",
    "pg_notification_queue_usage()",
    "pg_log_standby_snapshot()",
    "pg_stat_force_next_flush()",
    "public.f()",
    "my_side_effect()",
    # Accepted by the read-only role (no error, at most a warning), so the guard is all there is.
    "lo_create(0)",  # PG 15 only; 16+ refuse it in a read-only transaction
    "pg_ls_logdir()",  # granted to pg_monitor
    "lo_unlink(1)",  # PG 15 looks the object up before refusing the write
    # The fixture has no sequence; nextval/setval on a real one fail in a read-only
    # transaction, but the guard must not depend on that.
    "nextval('x')",
    "setval('x', 1)",
    "pg_terminate_backend(0)",
    "pg_notify('chan', 'x')",
    "pg_logical_emit_message(true, 'x', 'y')",
    "txid_current()",
    "pg_current_xact_id()",
    "hypopg_hide_index(1)",
    "pgstattuple('public.audit_log')",
    "pg_ls_waldir()",
]
_DB_REFUSED_FUNCTIONS = [
    "pg_reload_conf()",
    "pg_rotate_logfile()",
    "pg_switch_wal()",
    "pg_create_restore_point('x')",
    "pg_stat_reset()",
    "pg_stat_reset_shared('bgwriter')",
    "pg_stat_reset_single_table_counters('tenants'::regclass)",
    "pg_create_physical_replication_slot('x')",
    "pg_create_logical_replication_slot('x', 'pgoutput')",
    "pg_drop_replication_slot('x')",
    "pg_backup_start('x')",
    "pg_import_system_collations('pg_catalog')",
    "pg_stat_statements_reset()",
    "pg_stat_file('postgresql.conf')",
    "pg_read_binary_file('postgresql.conf')",
    "lo_import('/etc/hostname')",
    "pg_read_file('/etc/hostname')",
    "pg_ls_dir('.')",
]

FUNCTIONS = (
    _guard(*[f"SELECT {f}" for f in _FUNCTIONS])
    # The same calls hidden elsewhere in the tree: sleeps, advisory locks, cancel, set_config.
    + _guard(*[f"SELECT 1 FROM tenants WHERE {f} IS NOT NULL" for f in _FUNCTIONS[:12]])
    + _guard(*[f"SELECT * FROM tenants ORDER BY {f}" for f in _FUNCTIONS[:6]])
    + _guard(*[f"WITH x AS (SELECT {f}) SELECT 1 FROM x" for f in _FUNCTIONS[:6]])
    + _guard(*[f"SELECT (SELECT {f})" for f in _FUNCTIONS[3:9]])  # the advisory locks
    + _guard(*[f"SELECT * FROM tenants t, LATERAL (SELECT {f}) s" for f in _FUNCTIONS[:3]])
    + _guard("SELECT * FROM pg_sleep(0.01)", "SELECT * FROM pg_advisory_lock(1)")
    + _guard(
        "SELECT count(*) FROM pg_stat_activity WHERE pg_cancel_backend(pid)",
        "SELECT 1 WHERE 1 OPERATOR(public.=) 1",
        "SELECT pg_catalog.pg_sleep(0.01)",
        'SELECT "pg_sleep"(0.01)',
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid = 0",
        "SELECT PG_SLEEP(0.01)",
    )
    + _db(*[f"SELECT {f}" for f in _DB_REFUSED_FUNCTIONS])
)

# Statements the read-only role and transaction accept on PG 15-18 (verified by running them):
# ANALYZE only warns when the role does not own the table; transaction-control statements and
# SET TRANSACTION READ WRITE are allowed before the first query; LISTEN/NOTIFY are allowed in
# read-only transactions; PREPARE (of any statement) and EXPLAIN (without ANALYZE) do not check
# write privileges.
ACCEPTED_BY_DATABASE = _guard(
    # What these run depends on objects the analyzer does not control.
    "EXECUTE p",
    "EXPLAIN ANALYZE EXECUTE p",
    "CALL p()",
    "PREPARE TRANSACTION 'x'",
    "EXPLAIN SELECT nextval('x')",
    "ANALYZE tenants",
    "SET TRANSACTION READ WRITE",
    "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ WRITE",
    "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE",
    "BEGIN",
    "START TRANSACTION READ WRITE",
    "COMMIT",
    "LISTEN chan",
    "NOTIFY chan",
    "DO 'BEGIN NULL; END'",
    "EXPLAIN CREATE TABLE t AS SELECT 1",
    "PREPARE p AS DELETE FROM audit_log",
    "PREPARE p AS SELECT * FROM tenants FOR UPDATE",
    "PREPARE p AS INSERT INTO tenants VALUES (999, 'x', 'free')",
    "PREPARE p(bigint) AS UPDATE bookings SET amount = amount WHERE tenant_id = $1",
)

CORPUS: list[Forbidden] = (
    DML
    + WRITING_SELECTS
    + DDL
    + SESSION_AND_TRANSACTION
    + PROCEDURAL
    + MULTI_AND_MALFORMED
    + FUNCTIONS
    + ACCEPTED_BY_DATABASE
)
