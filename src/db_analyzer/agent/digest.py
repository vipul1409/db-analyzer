"""Compact, pre-digested tool results (proposal §6.2): ranked, units normalized, small."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from db_analyzer.analyzers.inventory import schema_rollup
from db_analyzer.core.model import Observation, ProbeResult, Run, StorageStats
from db_analyzer.core.units import format_bytes

UNREADABLE_SHOWN = 10
SQL_ROWS_SHOWN = 50
FINDINGS_SHOWN = 20
COUNTS_SHOWN = 50


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


def storage(
    run: Run, measured: list[StorageStats], observations: list[Observation], top_n: int
) -> dict[str, Any]:
    """`observations` in the analyzer's rank order; size facts are left to `largest`."""
    ranked = sorted(measured, key=lambda s: (-s.total_bytes, s.ref.qualified))
    total = sum(s.total_bytes for s in measured)
    problems = [o for o in observations if o.severity != "info"]
    return {
        "run_id": run.id,
        "run_status": run.status,
        "collections_measured": len(measured),
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
            for t in schema_rollup(measured)
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
                {"table": s.ref.qualified, "measurement": what, "reason": why}
                for s in ranked
                for what, why in s.skipped.items()
            ],
        ),
        "row_counts": (
            "rows_method 'estimate' = planner estimate (pg_class.reltuples), null = never "
            "analyzed; 'exact' = count(*)"
        ),
        "recency": "days since the last vacuum/analyze, manual or automatic; null = never",
    }


def exact_counts(run: Run, measured: list[StorageStats]) -> dict[str, Any]:
    counted = [s for s in measured if s.row_count_method == "exact"]
    return {
        "run_id": run.id,
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
