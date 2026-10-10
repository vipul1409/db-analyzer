"""Compact, pre-digested tool results (proposal §6.2): ranked, units normalized, small."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from db_analyzer.analyzers.inventory import schema_rollup
from db_analyzer.core.model import Observation, ProbeResult, WorkloadItem
from db_analyzer.core.run_view import RunView
from db_analyzer.core.units import BLOCK_BYTES, format_bytes, format_duration

UNREADABLE_SHOWN = 10
SQL_ROWS_SHOWN = 50
FINDINGS_SHOWN = 20
COUNTS_SHOWN = 50
STATEMENT_CHARS = 300


def probe(p: ProbeResult) -> dict[str, Any]:
    return {
        "server": f"PostgreSQL {p.server_version}",
        "role": "replica" if p.in_recovery else "primary",
        "host": p.host_type,
        "extensions": sorted(p.extensions),
        "pg_monitor": p.privileges.pg_monitor,
        "readable_tables": p.privileges.readable_tables,
        "unreadable_tables": p.privileges.unreadable_tables[:UNREADABLE_SHOWN],
        "statistics_reset_hours_ago": _hours_ago(p.stats.database_stats_reset),
        "never_analyzed_tables": p.stats.never_analyzed_tables,
    }


def storage(view: RunView, top_n: int) -> dict[str, Any]:
    """Problems in rank order; size facts are left to `largest`."""
    ranked = view.storage or []
    total = sum(s.total_bytes for s in ranked)
    problems = view.problems
    return {
        "run_id": view.run.id,
        "run_status": view.run.status,
        "collections_measured": len(ranked),
        "total_pretty": format_bytes(total),
        "findings": [
            {
                "rank": rank,
                "severity": o.severity,
                "finding": o.fingerprint,
                "title": o.title,
                "recommendation": o.recommendation,
            }
            for rank, o in enumerate(problems[:FINDINGS_SHOWN], start=1)
        ],
        **({"findings_shown": FINDINGS_SHOWN} if len(problems) > FINDINGS_SHOWN else {}),
        "schemas": [
            {
                "schema": t.schema,
                "tables": t.collections,
                "total_pretty": format_bytes(t.total_bytes),
                "share_of_total": t.share_of_total,
            }
            for t in schema_rollup(ranked)
        ],
        "largest": [
            {
                "rank": rank,
                "table": s.ref.qualified,
                "kind": s.ref.kind.value,
                **({"partitions": s.partitions} if s.partitions is not None else {}),
                "total_bytes": s.total_bytes,
                "total_pretty": format_bytes(s.total_bytes),
                "heap_pretty": format_bytes(s.data_bytes),
                "index_pretty": format_bytes(s.index_bytes),
                "toast_pretty": None if s.toast_bytes is None else format_bytes(s.toast_bytes),
                "rows": s.row_count,
                "rows_method": s.row_count_method,
                "last_vacuum_days_ago": _days_ago(
                    s.maintenance.last_vacuum if s.maintenance else None
                ),
                "last_analyze_days_ago": _days_ago(
                    s.maintenance.last_analyze if s.maintenance else None
                ),
            }
            for rank, s in enumerate(ranked[:top_n], start=1)
        ],
        **_shown(
            "skipped",
            [
                {"table": s.collection, "measurement": s.measurement, "reason": s.reason}
                for s in view.skipped
            ],
        ),
        "row_counts": (
            "rows_method 'estimate' = planner estimate (pg_class.reltuples), null = never "
            "analyzed; 'exact' = count(*)"
        ),
        "recency": "days since the last vacuum/analyze, manual or automatic; null = never",
    }


def exact_counts(view: RunView) -> dict[str, Any]:
    measured = view.storage or []
    counted = [s for s in measured if s.row_count_method == "exact"]
    return {
        "run_id": view.run.id,
        **_shown("counted", [{"table": s.ref.qualified, "rows": s.row_count} for s in counted]),
        **_shown(
            "skipped",
            [
                {"table": s.ref.qualified, "reason": s.skipped["exact_count"]}
                for s in measured
                if "exact_count" in s.skipped
            ],
        ),
    }


def top_queries(view: RunView, top_n: int) -> dict[str, Any]:
    """The workload ranking, costliest first, numbered so follow-ups can name a statement; or,
    without one, why not and the schema review made instead."""
    w = view.workload
    assert w is not None, "a workload Run"
    by_fingerprint = {o.fingerprint: o for o in view.observations}
    return {
        "run_id": view.run.id,
        "run_status": view.run.status,
        "source": w.source,
        "statistics_cover": None if w.window_seconds is None else format_duration(w.window_seconds),
        "statements": w.statements,
        "total_ms": round(w.total_ms, 1),
        **({"refused": w.refused} if w.refused else {}),
        **({"warnings": w.warnings} if w.warnings else {}),
        **({"enable_steps": w.enable_steps} if w.enable_steps else {}),
        "ranked": len(w.items),
        "queries": [
            _ranked(rank, i, by_fingerprint[f"slow_query:{i.fingerprint}"])
            for rank, i in enumerate(w.items[:top_n], start=1)
        ],
        **(
            {
                "schema_review": [
                    {
                        "severity": o.severity,
                        "finding": o.fingerprint,
                        "title": o.title,
                        "recommendation": o.recommendation,
                        "ddl": o.ddl,
                    }
                    for o in view.problems[:FINDINGS_SHOWN]
                ]
            }
            if w.source is None
            else {}
        ),
        "ranking": (
            "rank 1 = most total time; ranked_by = which rankings (total time, mean time, "
            "blocks read, temp blocks written) it is in the top 25 of"
        ),
    }


def _ranked(rank: int, i: WorkloadItem, o: Observation) -> dict[str, Any]:
    statement = i.text
    if len(statement) > STATEMENT_CHARS:
        statement = statement[: STATEMENT_CHARS - 1] + "…"
    return {
        "rank": rank,
        "severity": o.severity,
        "finding": o.fingerprint,
        "statement": statement,
        **_statement_stats(i, o),
        "plan_rules": [r["rule"] for r in o.evidence.get("plan_rules", [])],
        "explanation": o.recommendation,
    }


def _statement_stats(i: WorkloadItem, o: Observation) -> dict[str, Any]:
    skipped = o.evidence.get("plan_skipped")
    return {
        "calls": i.calls,
        "total_ms": round(i.total_ms, 1),
        "mean_ms": round(i.mean_ms, 2),
        "share_of_time": f"{i.share_of_time:.1%}",
        "ranked_by": i.ranked_by,
        **({"plan_skipped": skipped} if skipped else {}),
    }


def query_details(view: RunView, rank: int) -> dict[str, Any]:
    """One statement of a workload Run's ranking, with its plan and the plan rules that fired."""
    w = view.workload
    assert w is not None, "a workload Run"
    if w.refused:
        return {
            "run_id": view.run.id,
            "error": f"the latest workload Run ranked nothing: {w.refused}",
        }
    if not 1 <= rank <= len(w.items):
        return {
            "run_id": view.run.id,
            "error": f"rank {rank} is not in the latest ranking, of {len(w.items)} statements",
        }
    i = w.items[rank - 1]
    [o] = [o for o in view.observations if o.fingerprint == f"slow_query:{i.fingerprint}"]
    e = o.evidence
    return {
        "run_id": view.run.id,
        "ranked_at": view.run.started_at.isoformat(timespec="minutes"),
        "rank": rank,
        "of": len(w.items),
        "severity": o.severity,
        "finding": o.fingerprint,
        "title": o.title,
        "text": i.text,
        **_statement_stats(i, o),
        "rows": i.rows,
        "read_from_outside_shared_buffers": format_bytes(i.shared_blks_read * BLOCK_BYTES),
        "temp_files_written": format_bytes(i.temp_blks_written * BLOCK_BYTES),
        "plan": e.get("plan", []),
        "row_lookup_only": e.get("row_lookup_only", False),
        "plan_rules": e.get("plan_rules", []),
        "explanation": o.recommendation,
        "plan_estimates": "a generic plan: the planner's estimates, nothing was executed",
    }


def _shown(key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """The first COUNTS_SHOWN items, and the full count when that cuts any."""
    more = {f"{key}_total": len(items)} if len(items) > COUNTS_SHOWN else {}
    return {key: items[:COUNTS_SHOWN], **more}


def sql_result(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Rows as columns plus value lists; numbers stay numbers, anything else becomes text."""
    columns = list(rows[0]) if rows else []
    return {
        "columns": columns,
        "rows": [[_json_value(r[c]) for c in columns] for r in rows[:SQL_ROWS_SHOWN]],
        "row_count": len(rows),
        **({"rows_shown": SQL_ROWS_SHOWN} if len(rows) > SQL_ROWS_SHOWN else {}),
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _days_ago(at: datetime | None) -> float | None:
    return None if at is None else round((datetime.now(UTC) - at).total_seconds() / 86400, 1)


def _hours_ago(at: datetime | None) -> float | None:
    return None if at is None else round((datetime.now(UTC) - at).total_seconds() / 3600, 1)
