"""Postgres probe: what the analyzer can see on a Connection. All reads go through SafeExecutor."""

from datetime import UTC, datetime

from db_analyzer.core.model import HostType, Privileges, ProbeResult, StatsFreshness
from db_analyzer.safety.executor import SafeExecutor

ADVICE_SETTINGS = (
    "shared_preload_libraries",
    "track_io_timing",
    "pg_stat_statements.track",
    "log_min_duration_statement",
    "random_page_cost",
    "shared_buffers",
    "work_mem",
)

_SERVER = """
SELECT current_setting('server_version_num')::int AS version_num,
       current_setting('server_version') AS version,
       pg_is_in_recovery() AS in_recovery,
       current_setting('azure.extensions', true) AS azure_extensions
"""

_EXTENSIONS = """
SELECT e.extname, e.extversion, n.nspname AS schema
FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
"""

_ROLES = """
SELECT pg_has_role(current_user, 'pg_monitor', 'MEMBER') AS pg_monitor,
       pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER') AS pg_read_all_stats
"""

_TABLES = """
SELECT n.nspname || '.' || c.relname AS name,
       has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT') AS readable
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm') AND NOT c.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
ORDER BY 1
"""

_SETTINGS = "SELECT name, setting FROM pg_settings WHERE name IN ({names})".format(
    names=", ".join(f"'{n}'" for n in ADVICE_SETTINGS)
)

_DATABASE_STATS = """
SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()
"""

_ANALYZE_AGE = """
SELECT count(*) FILTER (WHERE last_analyze IS NULL AND last_autoanalyze IS NULL) AS never,
       min(greatest(last_analyze, last_autoanalyze)) AS oldest
FROM pg_stat_user_tables
"""

_STATEMENTS_STATS = "SELECT stats_reset FROM {schema}.pg_stat_statements_info"


def probe(executor: SafeExecutor) -> ProbeResult:
    def run(sql: str) -> list[dict[str, object]]:
        return executor.execute(sql, purpose="probe")

    server = run(_SERVER)[0]
    ext_rows = run(_EXTENSIONS)
    extensions = {str(r["extname"]): str(r["extversion"]) for r in ext_rows}
    ext_schema = {str(r["extname"]): str(r["schema"]) for r in ext_rows}
    roles = run(_ROLES)[0]
    tables = run(_TABLES)
    settings_rows = {str(r["name"]): str(r["setting"]) for r in run(_SETTINGS)}
    analyze = run(_ANALYZE_AGE)[0]
    # Reading pg_stat_statements errors unless the module is also preloaded (e.g. Azure before
    # the restart), so both must hold.
    preloaded = settings_rows.get("shared_preload_libraries") or ""
    statements_reset = (
        run(_STATEMENTS_STATS.format(schema=_ident(ext_schema["pg_stat_statements"])))[0][
            "stats_reset"
        ]
        if "pg_stat_statements" in extensions and "pg_stat_statements" in preloaded
        else None
    )
    # The GUC exists (possibly empty) only on Azure; elsewhere current_setting(..., true) is NULL.
    host_type: HostType = (
        "azure_flexible" if server["azure_extensions"] is not None else "self_managed"
    )

    return ProbeResult(
        server_version_num=_int(server["version_num"]),
        server_version=str(server["version"]),
        in_recovery=bool(server["in_recovery"]),
        host_type=host_type,
        extensions=extensions,
        privileges=Privileges(
            pg_monitor=bool(roles["pg_monitor"]),
            pg_read_all_stats=bool(roles["pg_read_all_stats"]),
            readable_tables=sum(1 for t in tables if t["readable"]),
            unreadable_tables=[str(t["name"]) for t in tables if not t["readable"]],
        ),
        settings={name: settings_rows.get(name) for name in ADVICE_SETTINGS},
        stats=StatsFreshness(
            database_stats_reset=_dt(run(_DATABASE_STATS)[0]["stats_reset"]),
            statements_stats_reset=_dt(statements_reset),
            never_analyzed_tables=_int(analyze["never"]),
            oldest_analyze=_dt(analyze["oldest"]),
        ),
        taken_at=datetime.now(UTC),
        extension_schemas=ext_schema,
    )


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _int(value: object) -> int:
    assert isinstance(value, int)
    return value


def _dt(value: object) -> datetime | None:
    assert value is None or isinstance(value, datetime)
    return value
