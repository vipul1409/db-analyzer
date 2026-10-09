"""Postgres workload collection: pg_stat_statements as the source, and the catalog reads of the
schema-only review that stands in when there is none."""

from db_analyzer.adapters.postgres.queries import LIBRARY
from db_analyzer.adapters.sql_common.templates import run_template
from db_analyzer.core.model import (
    CollectionKind,
    CollectionRef,
    ProbeResult,
    ScanActivity,
    UnindexedForeignKey,
    WorkloadSource,
    WorkloadStatement,
)
from db_analyzer.safety.executor import SafeExecutor

# Other roles' statements only, in this database: with pg_stat_statements.track = all the
# analyzer's own statements, including PREPARE of workload text, are recorded too, and must
# not be ranked or wrapped again (ADR 0002). Fetched whole and ranked in Python: statements
# merge by the hash of their text, which SQL cannot compute, and the source holds at most
# pg_stat_statements.max rows (5,000 by default).
_STATEMENTS = """
SELECT s.query, s.calls, s.total_exec_time, s.rows, s.shared_blks_read, s.temp_blks_written
FROM {schema}.pg_stat_statements s
WHERE s.dbid = (SELECT d.oid FROM pg_database d WHERE d.datname = current_database())
  AND s.userid <> (SELECT r.oid FROM pg_roles r WHERE r.rolname = current_user)
"""


def source(probe: ProbeResult) -> WorkloadSource | None:
    """The best workload source the Connection has. Reading pg_stat_statements errors unless the
    module is also preloaded (e.g. on Azure before the restart), so both must hold."""
    installed = "pg_stat_statements" in probe.extensions
    preloaded = "pg_stat_statements" in (probe.settings.get("shared_preload_libraries") or "")
    return "pg_stat_statements" if installed and preloaded else None


def statements(executor: SafeExecutor, probe: ProbeResult) -> list[WorkloadStatement]:
    schema = probe.extension_schemas["pg_stat_statements"].replace('"', '""')
    rows = executor.execute(_STATEMENTS.format(schema=f'"{schema}"'), purpose="workload")
    return [
        WorkloadStatement(
            text=str(r["query"]),
            calls=int(r["calls"]),
            total_ms=float(r["total_exec_time"]),
            rows=int(r["rows"]),
            shared_blks_read=int(r["shared_blks_read"]),
            temp_blks_written=int(r["temp_blks_written"]),
        )
        for r in rows
    ]


def unindexed_foreign_keys(
    executor: SafeExecutor, server_version_num: int
) -> list[UnindexedForeignKey]:
    template = LIBRARY.get("unindexed_foreign_keys", server_version_num)
    return [
        UnindexedForeignKey(
            table=CollectionRef(str(r["schema"]), str(r["name"]), CollectionKind(r["kind"])),
            constraint=str(r["constraint_name"]),
            columns=[str(c) for c in r["columns"]],
        )
        for r in run_template(executor, template, purpose="schema review")
    ]


def scan_activity(executor: SafeExecutor, server_version_num: int) -> list[ScanActivity]:
    template = LIBRARY.get("scan_activity", server_version_num)
    return [
        ScanActivity(
            table=CollectionRef(str(r["schema"]), str(r["name"]), CollectionKind(r["kind"])),
            seq_scans=int(r["seq_scans"]),
            seq_rows_read=int(r["seq_rows_read"]),
            idx_scans=int(r["idx_scans"]),
            live_rows=int(r["live_rows"]),
        )
        for r in run_template(executor, template, purpose="schema review")
    ]
