"""Inventory analyzer: store-agnostic findings from measured collection sizes."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from db_analyzer.core.model import (
    CollectionKind,
    Observed,
    Severity,
    StorageStats,
    UnknownCollections,
)
from db_analyzer.core.units import format_bytes

TOP_N = 10

# Bloat from the statistics counters: dead tuples per live tuple. Autovacuum's default trigger
# is 0.2.
BLOAT_MIN_RATIO = 0.2
BLOAT_MIN_DEAD_ROWS = 1_000
BLOAT_SEVERITY: list[tuple[float, Severity]] = [(1.0, "high"), (0.5, "medium"), (0.0, "low")]
# Bloat from a dead-tuple scan: the share of the table that is dead tuples or free space. Pruning
# turns dead tuples into free space that stays allocated; ~10% free is normal page slack.
BLOAT_MIN_WASTED_PERCENT = 30.0
BLOAT_WASTED_SEVERITY: list[tuple[float, Severity]] = [
    (50.0, "high"),
    (40.0, "medium"),
    (0.0, "low"),
]

# Stale statistics: rows changed since the last analyze, against the planner's estimate.
# Autoanalyze's default trigger is 0.1; twice that leaves it time to catch up.
STALE_MIN_CHANGED_SHARE = 0.2
STALE_MIN_ROWS = 1_000
STALE_OFF_BY = 10  # estimate this many times off the live count: medium, else low

# Index-heavy tables and oversized TOAST: below this size, page rounding decides the ratio.
NOISE_BELOW_BYTES = 1024 * 1024


def select(measured: list[StorageStats], names: list[str]) -> list[StorageStats]:
    """The collections named, schema-qualified or (when unambiguous) bare, in measured order."""
    by_name: dict[str, list[StorageStats]] = {}
    for s in measured:
        by_name.setdefault(s.ref.qualified, []).append(s)
        if s.ref.qualified != s.ref.name:
            by_name.setdefault(s.ref.name, []).append(s)
    wanted = {n.strip(): by_name.get(n.strip(), []) for n in names}
    if unknown := [n for n, found in wanted.items() if len(found) != 1]:
        raise UnknownCollections(unknown)
    chosen = {id(found[0]) for found in wanted.values()}
    return [s for s in measured if id(s) in chosen]


def needs_dead_tuple_scan(s: StorageStats) -> bool:
    """Whether the statistics counters suggest bloat worth confirming by reading the table."""
    m = s.maintenance
    return (
        s.ref.kind == CollectionKind.TABLE
        and m is not None
        and m.dead_rows >= BLOAT_MIN_DEAD_ROWS
        and m.dead_rows >= BLOAT_MIN_RATIO * max(m.live_rows, 1)
    )


@dataclass(frozen=True)
class SchemaTotal:
    """Sizes of every collection in one schema (namespace)."""

    schema: str | None
    collections: int
    data_bytes: int
    index_bytes: int
    toast_bytes: int
    total_bytes: int
    share_of_total: float


def schema_rollup(measured: list[StorageStats]) -> list[SchemaTotal]:
    """Per-schema totals, largest first."""
    grand_total = sum(s.total_bytes for s in measured) or 1
    by_schema: dict[str | None, list[StorageStats]] = {}
    for s in measured:
        by_schema.setdefault(s.ref.namespace, []).append(s)
    totals = [
        SchemaTotal(
            schema=schema,
            collections=len(items),
            data_bytes=sum(s.data_bytes for s in items),
            index_bytes=sum(s.index_bytes for s in items),
            toast_bytes=sum(s.toast_bytes or 0 for s in items),
            total_bytes=(total := sum(s.total_bytes for s in items)),
            share_of_total=round(total / grand_total, 4),
        )
        for schema, items in by_schema.items()
    ]
    return sorted(totals, key=lambda t: (-t.total_bytes, t.schema or ""))


SEVERITY_ORDER: dict[Severity, int] = {"high": 0, "medium": 1, "low": 2, "info": 3}


def analyze(
    measured: list[StorageStats],
    top_n: int = TOP_N,
    broad: bool = True,
    other_problems: Sequence[Observed] = (),
) -> list[Observed]:
    """Problems per collection, with `other_problems` (this Run's index health, for instance),
    ranked by severity then by the bytes involved, followed (for a broad Run only: a targeted
    one has nothing to rank against) by the largest collections as `size` Findings, which are
    facts worth reporting rather than problems."""
    found = [o for s in measured for o in _problems(s)] + list(other_problems)
    ranked = sorted(found, key=lambda o: (SEVERITY_ORDER[o.severity], -_bytes(o)))
    return ranked + (_largest(measured, top_n) if broad else [])


def _bytes(o: Observed) -> int:
    """What a problem weighs: its collection's total size, or its index's size."""
    return int(o.evidence.get("total_bytes", o.evidence.get("index_bytes", 0)))


def _largest(measured: list[StorageStats], top_n: int) -> list[Observed]:
    total = sum(s.total_bytes for s in measured)
    ranked = sorted((s for s in measured if s.total_bytes > 0), key=lambda s: -s.total_bytes)
    return [
        Observed(
            category="size",
            subject=s.ref.qualified,
            collection=s.ref.qualified,
            severity="info",
            title=f"{s.ref.qualified} is #{rank} by size: {format_bytes(s.total_bytes)}",
            evidence={
                "rank": rank,
                "total_bytes": s.total_bytes,
                "data_bytes": s.data_bytes,
                "index_bytes": s.index_bytes,
                "toast_bytes": s.toast_bytes,
                "share_of_total": round(s.total_bytes / total, 4),
                "row_count": s.row_count,
                "row_count_method": s.row_count_method,
            },
        )
        for rank, s in enumerate(ranked[:top_n], start=1)
    ]


def _problems(s: StorageStats) -> list[Observed]:
    found = (_bloat(s), _stale_stats(s), _index_heavy(s), _toast_oversized(s))
    return [o for o in found if o is not None]


def _bloat(s: StorageStats) -> Observed | None:
    """From a dead-tuple scan when there is one, else from the counters. Candidates for a scan
    come from the counters, so free space left after dead tuples were cleaned up is seen only
    where the counters still show dead tuples."""
    m, scan = s.maintenance, s.dead_tuple_scan
    if scan is not None:
        live, dead = scan.live_rows, scan.dead_rows
        wasted = round(scan.dead_percent + scan.free_percent, 2)
        if wasted < BLOAT_MIN_WASTED_PERCENT:
            return None
        severity = next(sev for floor, sev in BLOAT_WASTED_SEVERITY if wasted >= floor)
        title = (
            f"{s.ref.qualified} is {wasted:.0f}% dead tuples and free space "
            f"({format_bytes(s.data_bytes)} heap)"
        )
        evidence: dict[str, Any] = {
            "measured_by": "pgstattuple_approx",
            "wasted_percent": wasted,
            "dead_percent": scan.dead_percent,
            "free_percent": scan.free_percent,
        }
    elif m is not None:
        live, dead = m.live_rows, m.dead_rows
        ratio = dead / max(live, 1)
        if dead < BLOAT_MIN_DEAD_ROWS or ratio < BLOAT_MIN_RATIO:
            return None
        severity = next(sev for floor, sev in BLOAT_SEVERITY if ratio >= floor)
        title = f"{s.ref.qualified} has {ratio:.2f} dead tuples per live tuple ({dead:,} dead)"
        evidence = {"measured_by": "statistics counters"}
    else:
        return None
    autovacuum_off = m is not None and m.autovacuum_disabled
    return Observed(
        category="bloat",
        subject=s.ref.qualified,
        collection=s.ref.qualified,
        severity=severity,
        title=title,
        evidence={
            "dead_rows": dead,
            "live_rows": live,
            "dead_tuple_ratio": round(dead / max(live, 1), 4),
            **evidence,
            "autovacuum_disabled": autovacuum_off,
            "last_vacuum": _iso(m.last_vacuum) if m else None,
            "total_bytes": s.total_bytes,
        },
        recommendation=(
            f"Autovacuum is disabled on {s.ref.qualified}: re-enable it "
            "(ALTER TABLE ... RESET (autovacuum_enabled)), then VACUUM the table."
            if autovacuum_off
            else f"VACUUM {s.ref.qualified} and check why autovacuum is not keeping up."
        )
        + " Space already freed is reused, not returned: only a rewrite (VACUUM FULL, "
        "pg_repack) shrinks the table.",
    )


def stats_are_stale(
    *, analyzed: bool, live_rows: int, modified_since_analyze: int, estimate: int | None
) -> bool:
    """Whether a collection's planner statistics are stale: never analyzed though it holds rows,
    or changed by a large share since its last analyze."""
    if not analyzed:
        return live_rows >= STALE_MIN_ROWS
    return modified_since_analyze >= max(STALE_MIN_ROWS, STALE_MIN_CHANGED_SHARE * (estimate or 0))


def _stale_stats(s: StorageStats) -> Observed | None:
    m = s.maintenance
    if m is None:
        return None
    estimate = s.row_count if s.row_count_method == "estimate" else None
    never = m.last_analyze is None
    if not stats_are_stale(
        analyzed=not never,
        live_rows=m.live_rows,
        modified_since_analyze=m.modified_since_analyze,
        estimate=estimate,
    ):
        return None
    if never:
        title = f"{s.ref.qualified} has never been analyzed ({m.live_rows:,} live rows)"
        estimate_off = True
    else:
        changed = m.modified_since_analyze
        title = f"{s.ref.qualified} statistics are stale: {changed:,} rows changed since analyze"
        base = max(estimate or 0, 1)
        estimate_off = m.live_rows >= STALE_OFF_BY * base or m.live_rows * STALE_OFF_BY <= base
    return Observed(
        category="stale_stats",
        subject=s.ref.qualified,
        collection=s.ref.qualified,
        severity="medium" if estimate_off else "low",
        title=title,
        evidence={
            "estimated_rows": estimate,
            "live_rows": m.live_rows,
            "modified_since_analyze": m.modified_since_analyze,
            "last_analyze": _iso(m.last_analyze),
            "autovacuum_disabled": m.autovacuum_disabled,
            "total_bytes": s.total_bytes,
        },
        recommendation=(
            f"ANALYZE {s.ref.qualified}; planner estimates for it are unreliable until then"
            + (
                ", and autovacuum (which also analyzes) is disabled on it."
                if m.autovacuum_disabled
                else "."
            )
        ),
    )


def _index_heavy(s: StorageStats) -> Observed | None:
    if s.index_bytes <= s.data_bytes or s.index_bytes < NOISE_BELOW_BYTES:
        return None
    return Observed(
        category="size",
        subject=s.ref.qualified,
        collection=s.ref.qualified,
        rule="index_heavy",
        severity="low",
        title=(
            f"{s.ref.qualified} has more index than table: {format_bytes(s.index_bytes)} of "
            f"indexes on {format_bytes(s.data_bytes)} of heap"
        ),
        evidence={
            "index_bytes": s.index_bytes,
            "heap_bytes": s.data_bytes,
            "index_to_heap_ratio": round(s.index_bytes / max(s.data_bytes, 1), 2),
            "total_bytes": s.total_bytes,
        },
        recommendation=(
            "Normal for a narrow link table; otherwise look for unused, duplicate or "
            "overlapping indexes on it before adding more."
        ),
    )


def _toast_oversized(s: StorageStats) -> Observed | None:
    toast = s.toast_bytes or 0
    if toast <= s.data_bytes or toast < NOISE_BELOW_BYTES:
        return None
    return Observed(
        category="size",
        subject=s.ref.qualified,
        collection=s.ref.qualified,
        rule="toast_oversized",
        severity="low",
        title=(
            f"{s.ref.qualified} keeps most of its data in TOAST: {format_bytes(toast)} "
            f"against {format_bytes(s.data_bytes)} of heap"
        ),
        evidence={
            "toast_bytes": toast,
            "heap_bytes": s.data_bytes,
            "toast_to_heap_ratio": round(toast / max(s.data_bytes, 1), 2),
            "total_bytes": s.total_bytes,
        },
        recommendation=(
            "Large text, jsonb or bytea values dominate this table. Check which columns hold "
            "them; consider lz4 compression (ALTER TABLE ... ALTER COLUMN ... SET COMPRESSION "
            "lz4, for new values) or keeping large documents outside the database."
        ),
    )


def _iso(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()
