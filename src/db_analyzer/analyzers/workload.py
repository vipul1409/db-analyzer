"""Workload analyzer: ranks the most expensive statements of a workload source, and, when a
Connection has no source, reviews the schema instead (foreign keys without an index, tables read
mostly by sequential scans). Store-agnostic: sources hand over `WorkloadStatement`s."""

import dataclasses
import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from pglast import ast, parse_sql
from pglast.parser import ParseError

from db_analyzer.analyzers import plan_rules
from db_analyzer.analyzers.inventory import SEVERITY_ORDER
from db_analyzer.analyzers.plan_rules import PlanContext
from db_analyzer.core.model import (
    Observed,
    ScanActivity,
    Severity,
    StatementPlan,
    UnindexedForeignKey,
    WorkloadItem,
    WorkloadReport,
    WorkloadSource,
    WorkloadStatement,
    qualify,
)
from db_analyzer.core.units import BLOCK_BYTES, format_bytes, format_duration

TOP_N = 25  # statements kept per ranking
# Statistics younger than this say little about the workload. Per-Run configurable.
MIN_STATS_WINDOW = timedelta(hours=1)
# Statistics younger than this still rank, but may miss daily or weekly jobs.
SHORT_WINDOW = timedelta(hours=24)

# A statement is high or medium severity by its share of all the time the workload spent.
HIGH_SHARE = 0.25
MEDIUM_SHARE = 0.10

# A table is read mostly by sequential scans when it is big enough for that to matter and they
# outnumber its index scans. Medium when the scans fetched this many rows.
SEQ_MIN_SCANS = 50
SEQ_MIN_ROWS = 10_000
SEQ_MEDIUM_ROWS_READ = 10_000_000

# Plan nodes kept in a slow statement's evidence.
PLAN_LINES = 40
ROW_LOOKUP_ONLY = (
    "The plan covers only how it finds its rows: the analyzer may not EXPLAIN a write, so it "
    "planned the SELECT that finds them. The write itself, triggers and index maintenance add "
    "to that."
)

NO_PRIVILEGE_TEXT = "<insufficient privilege>"
HIDDEN_TEXT = "text hidden (no pg_read_all_stats)"
UNPARSEABLE_TEXT = "text unparseable (truncated?)"
UTILITY_STATEMENT = "utility statement"
_PLANNABLE = (ast.SelectStmt, ast.InsertStmt, ast.UpdateStmt, ast.DeleteStmt, ast.MergeStmt)
_RANKINGS: list[tuple[str, Callable[[WorkloadItem], float]]] = [
    ("total_time", lambda i: i.total_ms),
    ("mean_time", lambda i: i.mean_ms),
    ("blocks_read", lambda i: i.shared_blks_read),
    ("temp_blocks_written", lambda i: i.temp_blks_written),
]


def normalize(text: str) -> str:
    """The statement text a fingerprint is taken from: whitespace collapsed, no trailing
    semicolon. The source has already replaced values with placeholders."""
    return " ".join(text.split()).rstrip(";").rstrip()


def fingerprint(text: str) -> str:
    """Hash of the normalized text, never the server's query id, which changes across major
    versions and with object ids: the same statement keeps its Finding on any of them."""
    return hashlib.sha256(normalize(text).encode()).hexdigest()[:16]


def report(
    statements: Sequence[WorkloadStatement],
    *,
    source: WorkloadSource,
    stats_reset: datetime | None,
    now: datetime,
    on_replica: bool = False,
    min_window: timedelta = MIN_STATS_WINDOW,
    top_n: int = TOP_N,
) -> WorkloadReport:
    """Rank `statements` (one row per user and server-side id is fine: they are merged by
    fingerprint). Statements that are not plain queries or DML are left out and counted: a
    utility statement's text may carry values the source did not replace, and it has no plan
    to analyze. Nothing is ranked when the statistics are younger than `min_window`."""
    window = None if stats_reset is None else (now - stats_reset).total_seconds()
    included, excluded = _plannable(statements)
    merged = _merge(included)
    total_ms = sum(m.total_ms for m in merged)
    warnings = _warnings(window, min_window, on_replica, excluded)
    refused = None
    if window is not None and window < min_window.total_seconds():
        refused = (
            f"statistics were reset {format_duration(window)} ago, under the minimum window of "
            f"{format_duration(min_window.total_seconds())}: a ranking now would describe the last "
            "few minutes, not the workload. Rank again after more traffic, or lower the minimum."
        )
        warnings.insert(0, refused)
    return WorkloadReport(
        source=source,
        stats_reset=stats_reset,
        window_seconds=window,
        items=[] if refused else _rank(merged, total_ms, top_n),
        statements=len(merged),
        total_ms=total_ms,
        excluded=excluded,
        warnings=warnings,
        refused=refused,
    )


def unreadable(source: WorkloadSource, reason: str) -> WorkloadReport:
    """A source the Connection looked able to read, whose read failed (privilege denied, the
    EXPLAIN gate, an auxiliary session refused): nothing is ranked, and the report says why."""
    refused = f"{source} could not be read: {reason}"
    return WorkloadReport(
        source=source,
        stats_reset=None,
        window_seconds=None,
        items=[],
        statements=0,
        total_ms=0.0,
        excluded={},
        warnings=[refused],
        refused=refused,
    )


def no_source(steps: list[str]) -> WorkloadReport:
    return WorkloadReport(
        source=None,
        stats_reset=None,
        window_seconds=None,
        items=[],
        statements=0,
        total_ms=0.0,
        excluded={},
        warnings=["No workload source: slow statements cannot be ranked."],
        refused="no workload source",
        enable_steps=steps,
    )


def _plannable(
    statements: Sequence[WorkloadStatement],
) -> tuple[list[WorkloadStatement], dict[str, int]]:
    kept: list[WorkloadStatement] = []
    excluded: dict[str, int] = {}
    for s in statements:
        reason = _unplannable(s.text)
        if reason is None:
            kept.append(s)
        else:
            excluded[reason] = excluded.get(reason, 0) + 1
    return kept, excluded


def _unplannable(text: str) -> str | None:
    if text.strip() == NO_PRIVILEGE_TEXT:
        return HIDDEN_TEXT
    try:
        stmts = parse_sql(text)
    except ParseError:
        return UNPARSEABLE_TEXT
    if len(stmts) != 1 or not isinstance(stmts[0].stmt, _PLANNABLE):
        return UTILITY_STATEMENT
    return None


def _merge(statements: Sequence[WorkloadStatement]) -> list[WorkloadItem]:
    by_hash: dict[str, list[WorkloadStatement]] = {}
    for s in statements:
        by_hash.setdefault(fingerprint(s.text), []).append(s)
    merged = []
    for h, group in by_hash.items():
        calls = sum(s.calls for s in group)
        total = sum(s.total_ms for s in group)
        merged.append(
            WorkloadItem(
                fingerprint=h,
                text=normalize(group[0].text),
                calls=calls,
                total_ms=total,
                mean_ms=total / calls if calls else 0.0,
                rows=sum(s.rows for s in group),
                shared_blks_read=sum(s.shared_blks_read for s in group),
                temp_blks_written=sum(s.temp_blks_written for s in group),
                share_of_time=0.0,
                ranked_by=[],
            )
        )
    return merged


def _rank(merged: list[WorkloadItem], total_ms: float, top_n: int) -> list[WorkloadItem]:
    """The top `top_n` by each ranking (only statements with something to rank by), merged and
    deduplicated, most expensive first."""
    ranked_by: dict[str, list[str]] = {}
    for name, key in _RANKINGS:
        top = sorted((i for i in merged if key(i) > 0), key=lambda i: (-key(i), i.fingerprint))
        for i in top[:top_n]:
            ranked_by.setdefault(i.fingerprint, []).append(name)
    chosen = [
        dataclasses.replace(
            i,
            ranked_by=ranked_by[i.fingerprint],
            share_of_time=round(i.total_ms / total_ms, 4) if total_ms else 0.0,
        )
        for i in merged
        if i.fingerprint in ranked_by
    ]
    return sorted(chosen, key=lambda i: (-i.total_ms, i.fingerprint))


def _warnings(
    window: float | None, min_window: timedelta, on_replica: bool, excluded: dict[str, int]
) -> list[str]:
    found = []
    if window is None:
        found.append("The statistics reset time is unknown, so how long they cover is too.")
    elif min_window.total_seconds() <= window < SHORT_WINDOW.total_seconds():
        found.append(
            f"Statistics cover only {format_duration(window)}: jobs that run daily or weekly are "
            "missing from this ranking."
        )
    if on_replica:
        found.append(
            "These statistics were read on a replica, which counts only its own statements: "
            "the primary's workload is not here."
        )
    if hidden := excluded.get(HIDDEN_TEXT):
        found.append(
            f"{hidden} statements hide their text because the analyzer role cannot read other "
            "roles' statistics: GRANT pg_read_all_stats to it to rank them."
        )
    return found


def analyze(
    r: WorkloadReport,
    plans: Mapping[str, StatementPlan] | None = None,
    context: PlanContext | None = None,
) -> list[Observed]:
    """One `slow_query` Finding per ranked statement, fingerprinted by the hash of its text.
    `plans` holds the generic plan of each statement by fingerprint, where one was made; the
    plan rules read it with `context` to say why the statement is slow."""
    plans = plans or {}
    context = context or PlanContext(relations={})
    return [_slow_query(i, plans.get(i.fingerprint), context) for i in r.items]


def _slow_query(i: WorkloadItem, plan: StatementPlan | None, context: PlanContext) -> Observed:
    severity: Severity = (
        "high"
        if i.share_of_time >= HIGH_SHARE
        else "medium"
        if i.share_of_time >= MEDIUM_SHARE
        else "low"
    )
    hints = []
    if i.temp_blks_written:
        hints.append(
            f"It wrote {format_bytes(i.temp_blks_written * BLOCK_BYTES)} of temporary files: a "
            "sort, hash or aggregate spills to disk. Check work_mem and what it sorts or groups."
        )
    if "blocks_read" in i.ranked_by:
        hints.append(
            f"It read {format_bytes(i.shared_blks_read * BLOCK_BYTES)} from outside shared "
            "buffers: look for a missing index or a sequential scan on a large table."
        )
    evidence: dict[str, Any] = {
        "text": i.text,
        "calls": i.calls,
        "total_ms": round(i.total_ms, 3),
        "mean_ms": round(i.mean_ms, 3),
        "rows": i.rows,
        "shared_blks_read": i.shared_blks_read,
        "temp_blks_written": i.temp_blks_written,
        "share_of_time": i.share_of_time,
        "ranked_by": i.ranked_by,
    }
    recommendation = " ".join(hints) or None
    if plan is not None:
        evidence |= _plan_evidence(plan)
        reasons = (
            plan_rules.evaluate(plan.plan, context, temp_blks_written=i.temp_blks_written)
            if plan.plan
            else []
        )
        if reasons:
            evidence["plan_rules"] = [dataclasses.asdict(r) for r in reasons]
            # The plan says why: it replaces the hints guessed from the statistics alone.
            recommendation = " ".join(r.explanation for r in reasons)
        if plan.row_lookup_only:
            recommendation = " ".join(t for t in (ROW_LOOKUP_ONLY, recommendation) if t)
    return Observed(
        category="slow_query",
        subject=i.fingerprint,
        severity=severity,
        title=(
            f"{_head(i.text)}: {format_duration(i.total_ms / 1000)} in total over "
            f"{i.calls:,} calls ({i.mean_ms:.1f} ms each, {i.share_of_time:.0%} of the workload)"
        ),
        evidence=evidence,
        recommendation=recommendation,
    )


def _plan_evidence(p: StatementPlan) -> dict[str, Any]:
    if p.plan is None:
        return {"plan_skipped": p.skipped, "row_lookup_only": p.row_lookup_only}
    lines = p.plan.text()
    if len(lines) > PLAN_LINES:
        lines = [*lines[:PLAN_LINES], f"… {len(lines) - PLAN_LINES} more nodes"]
    return {"plan": lines, "row_lookup_only": p.row_lookup_only}


def schema_only_review(
    foreign_keys: list[UnindexedForeignKey], scans: list[ScanActivity]
) -> list[Observed]:
    """What the catalog alone shows, for a Connection with no workload source: foreign keys
    without an index, and tables that are read mostly by sequential scans."""
    heavy = {s.table.qualified: s for s in scans if is_seq_scan_heavy(s)}
    found = [_unindexed_fk(fk, heavy.get(fk.table.qualified)) for fk in foreign_keys]
    found += [_seq_scan_finding(s) for s in heavy.values()]
    return sorted(found, key=lambda o: (SEVERITY_ORDER[o.severity], o.fingerprint))


def is_seq_scan_heavy(s: ScanActivity) -> bool:
    return (
        s.seq_scans >= SEQ_MIN_SCANS and s.live_rows >= SEQ_MIN_ROWS and s.seq_scans > s.idx_scans
    )


def _seq_scan_finding(s: ScanActivity) -> Observed:
    table = s.table.qualified
    return Observed(
        category="missing_index",
        subject=table,
        collection=table,
        rule="seq_scan_heavy",
        severity="medium" if s.seq_rows_read >= SEQ_MEDIUM_ROWS_READ else "low",
        title=(
            f"{table} is read mostly by sequential scans: {s.seq_scans:,} against "
            f"{s.idx_scans:,} index scans ({s.seq_rows_read:,} rows fetched)"
        ),
        evidence={
            "table": table,
            "seq_scans": s.seq_scans,
            "seq_rows_read": s.seq_rows_read,
            "idx_scans": s.idx_scans,
            "live_rows": s.live_rows,
            "found_by": "schema review",
        },
        recommendation=(
            "Queries filter this table on columns no index covers. Without pg_stat_statements "
            "the analyzer cannot tell which: enable it to find the statements, or look at "
            "which columns your application filters and joins on."
        ),
    )


def _unindexed_fk(fk: UnindexedForeignKey, heavy: ScanActivity | None) -> Observed:
    columns = ", ".join(qualify(None, c) for c in fk.columns)
    table = fk.table.qualified
    return Observed(
        category="missing_index",
        subject=f"{table}({','.join(fk.columns)})",
        collection=table,
        severity="medium" if heavy else "low",
        title=f"{table} has no index starting with its foreign key ({', '.join(fk.columns)})",
        evidence={
            "table": table,
            "constraint": fk.constraint,
            "columns": fk.columns,
            "seq_scan_heavy": heavy is not None,
            "found_by": "schema review",
        },
        recommendation=(
            f"Deleting or updating a row of the table {fk.constraint} references scans all of "
            f"{table}, and joins on the key cannot use an index. Index it unless the table is "
            "tiny or those rows are never deleted or updated."
        ),
        ddl=f"CREATE INDEX CONCURRENTLY ON {table} ({columns});",
    )


def _head(text: str, chars: int = 80) -> str:
    return text if len(text) <= chars else text[: chars - 1] + "…"
