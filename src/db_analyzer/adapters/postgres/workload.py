"""Postgres workload collection: pg_stat_statements and Azure Query Store as sources, and the
catalog reads of the schema-only review that stands in when there is none."""

from datetime import UTC, datetime

from pglast.parser import ParseError, Token, scan

from db_analyzer.adapters.postgres.probe import MAX_QUERY_TEXT_LENGTH, QUERY_CAPTURE_MODE
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
from db_analyzer.safety.executor import Row, SafeExecutor

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
            [_statement(r, str(r["query"])) for r in rows],
            stats_reset=probe.stats.statements_stats_reset,
        )


PG_STAT_STATEMENTS = PgStatStatements()


# This database's statements by other roles than ours, left out as for pg_stat_statements,
# and not Azure's own (is_system_query). One row per user, database, query id and interval:
# summed per query id here, merged by fingerprint by the analyzer. `since` is the start of the
# oldest interval read. The database oid is inlined: it is an integer read from the catalog.
_QUERY_STORE = """
SELECT q.query_sql_text AS query, sum(q.calls) AS calls, sum(q.total_time) AS total_exec_time,
       sum(q.rows) AS rows, sum(q.shared_blks_read) AS shared_blks_read,
       sum(q.temp_blks_written) AS temp_blks_written, min(q.start_time) AS since
FROM query_store.qs_view q
WHERE q.db_id = {db_oid} AND NOT q.is_system_query
  AND q.user_id <> (SELECT r.oid FROM pg_roles r WHERE r.rolname = current_user)
GROUP BY q.query_id, q.query_sql_text
"""
_DATABASE_OID = "SELECT d.oid FROM pg_database d WHERE d.datname = current_database()"
AZURE_SYS = "azure_sys"
DEFAULT_MAX_TEXT_LENGTH = 6000  # pg_qs.max_query_text_length when it cannot be read

# Stands in for a statement whose values could not be replaced, or whose text was cut off: its
# text is never read further, and the analyzer counts it as unparseable.
UNREADABLE_TEXT = "<values could not be replaced>"


class QueryStore:
    """Azure Query Store as a workload source, read from the azure_sys database through an
    auxiliary session of the Connection. Its statistics cover the retention period
    (pg_qs.retention_period_in_days), so the window starts at the oldest interval it holds, and
    the interval still being recorded is not visible yet. Its text is the first run of each
    statement, values included, so they are replaced by placeholders before anything ranks or
    stores it."""

    name: WorkloadSource = "azure_query_store"

    def enable_steps(self, probe: ProbeResult) -> list[str]:
        if probe.host_type != "azure_flexible":
            return ["Query Store exists only on Azure Database for PostgreSQL flexible server."]
        steps = []
        if (probe.settings.get(QUERY_CAPTURE_MODE) or "none") == "none":
            steps.append(
                f"Set the server parameter {QUERY_CAPTURE_MODE} to top (Azure portal: Server "
                "parameters). Query Store persists its first data after up to 20 minutes."
            )
        if not probe.privileges.azure_sys_connect:
            steps.append(f"GRANT CONNECT ON DATABASE {AZURE_SYS} TO <analyzer role>;")
        return steps

    def read(self, executor: SafeExecutor, probe: ProbeResult) -> WorkloadReading:
        [database] = executor.execute(_DATABASE_OID, purpose="workload")
        with executor.auxiliary(AZURE_SYS) as azure_sys_executor:
            rows = azure_sys_executor.execute(
                _QUERY_STORE.format(db_oid=int(database["oid"])), purpose="workload"
            )
        max_length = int(probe.settings.get(MAX_QUERY_TEXT_LENGTH) or DEFAULT_MAX_TEXT_LENGTH)
        starts = [_utc(r["since"]) for r in rows if r["since"] is not None]
        return WorkloadReading(
            [_statement(r, _readable(str(r["query"]), max_length)) for r in rows],
            stats_reset=min(starts, default=None),
        )


QUERY_STORE = QueryStore()


def _readable(text: str, max_length: int) -> str:
    """Query Store text with its values replaced, or UNREADABLE_TEXT when it was cut off at
    the maximum length (in bytes): what is left may still parse, as another statement."""
    if len(text.encode()) >= max_length:
        return UNREADABLE_TEXT
    return replace_constants(text) or UNREADABLE_TEXT


def _statement(row: Row, text: str) -> WorkloadStatement:
    return WorkloadStatement(
        text=text,
        calls=int(row["calls"]),
        total_ms=float(row["total_exec_time"]),
        rows=int(row["rows"]),
        shared_blks_read=int(row["shared_blks_read"]),
        temp_blks_written=int(row["temp_blks_written"]),
    )


_CONSTANTS = frozenset({"SCONST", "USCONST", "BCONST", "XCONST", "ICONST", "FCONST"})
_NUMBERS = frozenset({"ICONST", "FCONST"})
# Tokens that end an operand: a minus after one subtracts, anywhere else it is a sign. Keywords
# that are not reserved may be column names, so they end operands too.
_OPERAND_END = frozenset({"IDENT", "PARAM", "ASCII_41", "ASCII_93"}) | _CONSTANTS


def replace_constants(text: str) -> str | None:
    """`text` with each literal value replaced by a placeholder numbered after the highest one
    already in it, as pg_stat_statements does; a negative number is one value. None when the
    text cannot be scanned (cut off inside a literal), since a value could then remain."""
    try:
        tokens = scan(text)
    except ParseError:
        return None
    n = max((int(text[t.start + 1 : t.end + 1]) for t in tokens if t.name == "PARAM"), default=0)
    out: list[str] = []
    last = 0
    for i, t in enumerate(tokens):
        if t.name not in _CONSTANTS:
            continue
        start = t.start
        if (
            t.name in _NUMBERS
            and i > 0
            and _is_sign(tokens[i - 1], tokens[i - 2] if i > 1 else None, t.start)
        ):
            start = tokens[i - 1].start
        n += 1
        out += [text[last:start], f"${n}"]
        last = t.end + 1
    out.append(text[last:])
    return "".join(out)


def _is_sign(minus: Token, before: Token | None, number_start: int) -> bool:
    if minus.name != "ASCII_45" or minus.end + 1 != number_start:
        return False
    if before is None:
        return True
    operand = before.name in _OPERAND_END or before.kind in (
        "UNRESERVED_KEYWORD",
        "COL_NAME_KEYWORD",
        "TYPE_FUNC_NAME_KEYWORD",
    )
    return not operand


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


def _utc(value: object) -> datetime:
    assert isinstance(value, datetime)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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
