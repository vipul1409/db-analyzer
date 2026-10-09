"""Markdown report of one Run."""

from db_analyzer.core.model import Connection, Observation, Run, StorageStats
from db_analyzer.core.units import format_bytes


def markdown(
    connection: Connection,
    run: Run,
    measured: list[StorageStats],
    observations: list[Observation],
) -> str:
    finished = run.finished_at.strftime("%Y-%m-%d %H:%M UTC") if run.finished_at else "-"
    lines = [
        f"# Inventory report: {connection.name}",
        "",
        f"Run `{run.id}` · {run.status} · finished {finished}",
        "",
        "## Tables by size",
        "",
        "| # | Table | Kind | Total | Heap | Indexes | TOAST | Rows |",
        "|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    ranked = sorted(measured, key=lambda s: (-s.total_bytes, s.ref.qualified))
    for rank, s in enumerate(ranked, start=1):
        toast = format_bytes(s.toast_bytes) if s.toast_bytes is not None else "-"
        rows = "unknown" if s.row_count is None else f"{s.row_count:,}"
        if s.row_count is not None and s.row_count_method != "exact":
            rows += f" ({s.row_count_method})"
        lines.append(
            f"| {rank} | {s.ref.qualified} | {s.ref.kind.value.replace('_', ' ')} "
            f"| {format_bytes(s.total_bytes)} | {format_bytes(s.data_bytes)} "
            f"| {format_bytes(s.index_bytes)} | {toast} | {rows} |"
        )
    if observations:
        lines += ["", "## Findings", ""]
        lines += [f"- **{o.severity}** `{o.fingerprint}`: {o.title}" for o in observations]
    if any(run.skipped.values()):
        lines += ["", "## Skipped", ""]
        lines += [
            f"- {analyzer}: {ref.qualified}: {why}"
            for analyzer, items in run.skipped.items()
            for ref, why in items
        ]
    return "\n".join(lines) + "\n"
