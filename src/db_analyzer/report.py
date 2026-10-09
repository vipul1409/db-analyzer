"""Reports of one Run: Markdown to read, JSON to diff and process."""

import dataclasses
import json
from datetime import datetime
from typing import Any

from db_analyzer.analyzers.inventory import schema_rollup
from db_analyzer.core.model import Connection, Observation, Run, StorageStats, WorkloadReport
from db_analyzer.core.units import format_bytes


def markdown(
    connection: Connection,
    run: Run,
    measured: list[StorageStats],
    observations: list[Observation],
    workload: WorkloadReport | None = None,
) -> str:
    """`observations` in rank order, most severe first (as the analyzer records them)."""
    finished = run.finished_at.strftime("%Y-%m-%d %H:%M UTC") if run.finished_at else "-"
    title = "Inventory" if "inventory" in run.scope else "Workload"
    lines = [
        f"# {title} report: {connection.name}",
        "",
        f"Run `{run.id}` · {run.status} · finished {finished}",
    ]
    if "inventory" in run.scope:
        lines += _inventory_tables(measured)
    if workload is not None:
        lines += _workload_section(workload)
    ranked = sorted(measured, key=lambda s: (-s.total_bytes, s.ref.qualified))
    if observations:
        lines += ["", "## Findings", ""]
        for rank, o in enumerate(observations, start=1):
            lines.append(f"{rank}. **{o.severity}** `{o.fingerprint}`: {o.title}")
            if o.recommendation:
                lines.append(f"   {o.recommendation}")
            if o.ddl:
                lines.append(f"   `{o.ddl}`")
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


def _inventory_tables(measured: list[StorageStats]) -> list[str]:
    lines = [
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
    return lines


def _workload_section(w: WorkloadReport) -> list[str]:
    lines = ["", "## Workload", ""]
    if w.source is None:
        lines.append("No workload source: slow statements could not be ranked. To enable one:")
        lines += [f"{n}. {step}" for n, step in enumerate(w.enable_steps, start=1)]
        lines.append("")
        lines.append("The review below uses the schema alone.")
        return lines
    reset = "-" if w.stats_reset is None else w.stats_reset.strftime("%Y-%m-%d %H:%M UTC")
    lines.append(
        f"Source: {w.source} · statistics reset {reset} · {w.statements:,} statements, "
        f"{w.total_ms / 1000:,.1f} s in total"
    )
    lines += [f"- Warning: {warning}" for warning in w.warnings]
    lines += [f"- Left out: {n} with {why}" for why, n in sorted(w.excluded.items())]
    if w.items:
        lines += [
            "",
            "| # | Fingerprint | Calls | Total ms | Mean ms | Share | Blocks read | Temp blocks |"
            " Statement |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        lines += [
            f"| {rank} | `{i.fingerprint}` | {i.calls:,} | {i.total_ms:,.1f} | {i.mean_ms:,.2f} "
            f"| {i.share_of_time:.1%} | {i.shared_blks_read:,} | {i.temp_blks_written:,} "
            f"| {_cell(i.text)} |"
            for rank, i in enumerate(w.items, start=1)
        ]
    return lines


def json_export(
    connection: Connection,
    run: Run,
    measured: list[StorageStats],
    observations: list[Observation],
    workload: WorkloadReport | None = None,
) -> str:
    """Collections by name and Findings by fingerprint, keys sorted, one value per line: two
    exports line up, so a diff shows only what changed. Like the Markdown report, it holds what
    the Run saw, not Finding statuses, which change after the Run."""
    doc: dict[str, Any] = {
        "connection": connection.name,
        "run": {
            "id": run.id,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "scope": {a: sorted(r.qualified for r in refs) for a, refs in run.scope.items()},
            "skipped": {
                a: [{"collection": r.qualified, "reason": why} for r, why in items]
                for a, items in run.skipped.items()
            },
        },
        "collections": [
            {"collection": s.ref.qualified, **_without_ref(s)}
            for s in sorted(measured, key=lambda s: s.ref.qualified)
        ],
        "findings": [
            {
                "fingerprint": o.fingerprint,
                "severity": o.severity,
                "title": o.title,
                "evidence": o.evidence,
                "recommendation": o.recommendation,
                "ddl": o.ddl,
            }
            for o in sorted(observations, key=lambda o: o.fingerprint)
        ],
    }
    if workload is not None:
        doc["workload"] = {
            **dataclasses.asdict(workload),
            "items": sorted(
                (dataclasses.asdict(i) for i in workload.items), key=lambda i: i["fingerprint"]
            ),
        }
    return json.dumps(doc, indent=2, sort_keys=True, default=_json_value) + "\n"


def _without_ref(s: StorageStats) -> dict[str, Any]:
    data = dataclasses.asdict(s)
    data["kind"] = s.ref.kind.value
    del data["ref"]
    return data


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _day(at: datetime | None) -> str:
    return "never" if at is None else at.strftime("%Y-%m-%d")


def _cell(text: str) -> str:
    """Statement text as one Markdown table cell: pipes escaped, backticks as a code span that
    cannot be closed early."""
    return (
        "<code>"
        + text.replace("&", "&amp;").replace("<", "&lt;").replace("|", "&#124;")
        + "</code>"
    )
