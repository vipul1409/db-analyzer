"""Opening a hardened Postgres session for a Connection.

Order matters: refuse unsupported versions, then verify the login itself is read-only (before
our own session settings could mask a writable role), then harden the session.
"""

from typing import Any

import psycopg

from db_analyzer.core.model import ConnectionRefused, SessionLimits

MIN_SERVER_VERSION_NUM = 150000
APPLICATION_NAME = "db-analyzer"


def open_session(dsn: str, limits: SessionLimits) -> psycopg.Connection[Any]:
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    try:
        _refuse_unsupported_version(conn)
        _verify_role_is_read_only(conn)
        _harden(conn, limits)
    except BaseException:
        conn.close()
        raise
    return conn


def _refuse_unsupported_version(conn: psycopg.Connection[Any]) -> None:
    version = conn.info.server_version
    if version < MIN_SERVER_VERSION_NUM:
        raise ConnectionRefused(
            f"PostgreSQL {version // 10000} is not supported; version 15 or later is required"
        )


_WRITE_PRIVILEGES = """
WITH user_schemas AS (
  SELECT oid FROM pg_namespace
  WHERE nspname NOT IN ('pg_catalog', 'information_schema')
    AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp%'
)
SELECT array_remove(ARRAY[
  CASE WHEN pg_has_role(current_user, 'pg_write_all_data', 'MEMBER')
       THEN 'member of pg_write_all_data' END,
  CASE WHEN has_database_privilege(current_database(), 'CREATE')
       THEN 'CREATE on the database' END,
  CASE WHEN EXISTS (SELECT 1 FROM user_schemas s WHERE has_schema_privilege(s.oid, 'CREATE'))
       THEN 'CREATE on a schema' END,
  CASE WHEN EXISTS (
         SELECT 1 FROM pg_class c JOIN user_schemas s ON s.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND (has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
                OR has_any_column_privilege(c.oid, 'INSERT, UPDATE')))
       THEN 'write privileges on a table' END
], NULL)
"""


def _verify_role_is_read_only(conn: psycopg.Connection[Any]) -> None:
    """Startup self-check. Runs outside SafeExecutor on purpose.

    `default_transaction_read_only` is only a default a session can switch off, so the role's
    privileges are checked directly; then a harmless write must fail. The temp table lives only
    inside a transaction that is always rolled back.
    """
    row = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
    if row is not None and row[0]:
        raise ConnectionRefused("role is not read-only: it is a superuser")
    granted = conn.execute(_WRITE_PRIVILEGES).fetchone()
    if granted is not None and granted[0]:
        raise ConnectionRefused(f"role is not read-only: it can write ({', '.join(granted[0])})")
    try:
        with conn.transaction(force_rollback=True):
            conn.execute("CREATE TEMP TABLE db_analyzer_write_probe (x int)")
    except psycopg.errors.ReadOnlySqlTransaction:
        return
    except psycopg.errors.InsufficientPrivilege:
        return
    raise ConnectionRefused("role is not read-only: a write probe succeeded")


def _harden(conn: psycopg.Connection[Any], limits: SessionLimits) -> None:
    settings = {
        "default_transaction_read_only": "on",
        "statement_timeout": limits.statement_timeout,
        "lock_timeout": limits.lock_timeout,
        "application_name": APPLICATION_NAME,
        "work_mem": limits.work_mem,
    }
    for name, value in settings.items():
        conn.execute("SELECT set_config(%s, %s, false)", (name, value))
