"""Compact, pre-digested tool results (proposal §6.2): ranked, units normalized, small."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from db_analyzer.core.model import ProbeResult, Run, StorageStats
from db_analyzer.core.units import format_bytes

UNREADABLE_SHOWN = 10
SQL_ROWS_SHOWN = 50


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


def storage(run: Run, measured: list[StorageStats], top_n: int) -> dict[str, Any]:
    ranked = sorted(measured, key=lambda s: (-s.total_bytes, s.ref.qualified))
    total = sum(s.total_bytes for s in measured)
    return {
        "run_id": run.id,
        "run_status": run.status,
        "collections_measured": len(measured),
        "total_pretty": format_bytes(total),
        "largest": [
            {
                "rank": rank,
                "table": s.ref.qualified,
                "kind": s.ref.kind.value,
                "total_bytes": s.total_bytes,
                "total_pretty": format_bytes(s.total_bytes),
                "heap_pretty": format_bytes(s.data_bytes),
                "index_pretty": format_bytes(s.index_bytes),
                "toast_pretty": None if s.toast_bytes is None else format_bytes(s.toast_bytes),
                "estimated_rows": s.row_count,
            }
            for rank, s in enumerate(ranked[:top_n], start=1)
        ],
        "row_counts": "planner estimates (pg_class.reltuples); null = never analyzed",
    }


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


def _hours_ago(at: datetime | None) -> float | None:
    return None if at is None else round((datetime.now(UTC) - at).total_seconds() / 3600, 1)
