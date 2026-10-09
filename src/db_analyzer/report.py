"""Markdown report of one Run."""

from datetime import datetime

from db_analyzer.analyzers.inventory import schema_rollup
from db_analyzer.core.model import Connection, Observation, Run, StorageStats
from db_analyzer.core.units import format_bytes


def markdown(
    connection: Connection,
    run: Run,
    measured: list[StorageStats],
    observations: list[Observation],
) -> str:
    """`observations` in rank order, most severe first (as the analyzer records them)."""
    finished = run.finished_at.strftime("%Y-%m-%d %H:%M UTC") if run.finished_at else "-"
    lines = [
        f"# Inventory report: {connection.name}",
        "",
        f"Run `{run.id}` · {run.status} · finished {finished}",
        "",
        "## Tables by size",
        "",
        "| # | Table | Kind | Total | Heap | Indexes | TOAST | Rows | Last vacuum | Last analyze |",
        "|---:|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    ranked = sorted(measured, key=lambda s: (-s.total_bytes, s.ref.qualified))
    for rank, s in enumerate(ranked, start=1):
        toast = format_bytes(s.toast_bytes) if s.toast_bytes is not None else "-"
        rows = "unknown" if s.row_count is None else f"{s.row_count:,} ({s.row_count_method})"
        kind = s.ref.kind.value.replace("_", " ")
        if s.partitions is not None:
            kind += f" ({s.partitions} partitions)"
        m = s.maintenance
        lines.append(
            f"| {rank} | {s.ref.qualified} | {kind} "
            f"| {format_bytes(s.total_bytes)} | {format_bytes(s.data_bytes)} "
            f"| {format_bytes(s.index_bytes)} | {toast} | {rows} "
            f"| {_day(m.last_vacuum if m else None)} | {_day(m.last_analyze if m else None)} |"
        )
    lines += [
        "",
        "## Schemas",
        "",
        "| Schema | Tables | Total | Heap | Indexes | TOAST | Share |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {t.schema or '-'} | {t.collections} | {format_bytes(t.total_bytes)} "
        f"| {format_bytes(t.data_bytes)} | {format_bytes(t.index_bytes)} "
        f"| {format_bytes(t.toast_bytes)} | {t.share_of_total:.1%} |"
        for t in schema_rollup(measured)
    ]
    if observations:
        lines += ["", "## Findings", ""]
        for rank, o in enumerate(observations, start=1):
            lines.append(f"{rank}. **{o.severity}** `{o.fingerprint}`: {o.title}")
            if o.recommendation:
                lines.append(f"   {o.recommendation}")
    skipped = [
        f"- {analyzer}: {ref.qualified}: {why}"
        for analyzer, items in run.skipped.items()
        for ref, why in items
    ] + [
        f"- {s.ref.qualified}: {what.replace('_', ' ')} skipped: {why}"
        for s in ranked
        for what, why in s.skipped.items()
    ]
    if skipped:
        lines += ["", "## Skipped", "", *skipped]
    return "\n".join(lines) + "\n"


def _day(at: datetime | None) -> str:
    return "never" if at is None else at.strftime("%Y-%m-%d")
