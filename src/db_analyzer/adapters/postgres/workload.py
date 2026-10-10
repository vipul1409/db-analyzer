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
    WorkloadReading,
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


class PgStatStatements:
    """pg_stat_statements as a workload source. Reading it errors unless the module is also
    preloaded (e.g. on Azure before the restart), so both must hold before it is read."""

    name: WorkloadSource = "pg_stat_statements"

    def enable_steps(self, probe: ProbeResult) -> list[str]:
        installed = "pg_stat_statements" in probe.extensions
        preloaded = "pg_stat_statements" in (probe.settings.get("shared_preload_libraries") or "")
        if installed and preloaded:
            return []
        return enable_steps(installed, preloaded, probe.host_type == "azure_flexible")

    def read(self, executor: SafeExecutor, probe: ProbeResult) -> WorkloadReading:
        schema = probe.extension_schemas["pg_stat_statements"].replace('"', '""')
        rows = executor.execute(_STATEMENTS.format(schema=f'"{schema}"'), purpose="workload")
        return WorkloadReading(
            [
                WorkloadStatement(
                    text=str(r["query"]),
                    calls=int(r["calls"]),
                    total_ms=float(r["total_exec_time"]),
                    rows=int(r["rows"]),
                    shared_blks_read=int(r["shared_blks_read"]),
                    temp_blks_written=int(r["temp_blks_written"]),
                )
                for r in rows
            ],
            stats_reset=probe.stats.statements_stats_reset,
        )


PG_STAT_STATEMENTS = PgStatStatements()


def enable_steps(installed: bool, preloaded: bool, azure: bool) -> list[str]:
    """What to do, in order, to get pg_stat_statements working."""
    steps = []
    if not preloaded:
        steps.append(
            "Add pg_stat_statements to the server parameter shared_preload_libraries"
            + (" (Azure portal: Server parameters), then restart the server." if azure else ",")
            + ("" if azure else " then restart PostgreSQL: it is only read at startup.")
        )
    if not installed:
        steps.append(
            "In this database, as a role allowed to: CREATE EXTENSION pg_stat_statements;"
            + (" On Azure, allow it in azure.extensions first." if azure else "")
        )
    steps.append(
        "Let the analyzer role read other roles' statements: GRANT pg_read_all_stats TO "
        "<analyzer role>; (pg_monitor includes it)."
    )
    steps.append(
        "Optionally set pg_stat_statements.track = all (the default, top, skips statements "
        "run inside functions) and track_io_timing = on."
    )
    steps.append("Rank the workload again after a day or so of normal traffic.")
    return steps


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
