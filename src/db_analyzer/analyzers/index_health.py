"""Index health: unused, duplicate or overlapping, and invalid indexes, from the catalog.

Each index is reported at most once, invalid first: an invalid index is left out of the other
checks, and an index reported as a duplicate or overlapping is not also reported as unused."""

from datetime import datetime
from typing import Any

from db_analyzer.core.model import FindingCategory, IndexStats, Observed, Severity
from db_analyzer.core.units import format_bytes

# An unused index this large is worth dropping soon: it costs every write and the cache.
UNUSED_LARGE_BYTES = 100 * 1024 * 1024
# Statistics younger than this may not have seen a monthly job that needs the index.
SHORT_STATS_WINDOW_DAYS = 30


def analyze(
    indexes: list[IndexStats], stats_reset: datetime | None, now: datetime, on_replica: bool
) -> list[Observed]:
    """`stats_reset`: when the scan counters were last reset, None if never. `on_replica`: the
    counters were read on a replica, which counts its own scans only."""
    found = [_invalid(i) for i in indexes if not i.valid]
    valid = [i for i in indexes if i.valid]

    groups: dict[tuple[Any, ...], list[IndexStats]] = {}
    for i in valid:
        key = (
            i.table.qualified,
            i.method,
            tuple(i.keys),
            tuple(i.include),
            i.predicate,
            i.nulls_not_distinct,
        )
        groups.setdefault(key, []).append(i)
    in_duplicates: set[str] = set()
    reported: set[str] = set()
    for members in (g for g in groups.values() if len(g) > 1):
        keep, *extra = sorted(
            members, key=lambda i: (not i.primary, not i.constraint, not i.unique, i.name)
        )
        in_duplicates.update(i.name for i in members)
        for i in extra:
            found.append(_duplicate(i, keep))
            reported.add(i.name)

    for i in valid:
        kept = [w for w in valid if w.name not in reported]
        if i.name not in reported and (wider := _wider(i, kept)) is not None:
            found.append(_overlapping(i, wider))
            reported.add(i.name)

    window = _days(stats_reset, now)
    for i in valid:
        never_used = i.scans == 0 and not (i.unique or i.primary or i.constraint)
        if never_used and i.name not in in_duplicates | reported:
            found.append(_unused(i, stats_reset, window, on_replica))
    return found


def _wider(i: IndexStats, valid: list[IndexStats]) -> IndexStats | None:
    """The widest btree on the same table whose keys start with all of `i`'s, if any: being
    widest, it is never itself overlapping, so it is the one to keep. A unique `i` enforces
    uniqueness that the wider index does not, so it is never overlapped."""
    if i.method != "btree" or i.unique:
        return None
    candidates = [
        w
        for w in valid
        if w.table == i.table
        and w.method == "btree"
        and w.predicate == i.predicate
        and len(w.keys) > len(i.keys)
        and w.keys[: len(i.keys)] == i.keys
    ]
    return min(candidates, key=lambda w: (-len(w.keys), w.name), default=None)


def _invalid(i: IndexStats) -> Observed:
    if i.partitioned:
        return _observed(
            i,
            "invalid_index",
            "medium",
            f"{i.name} is invalid: some partitions have no matching index attached",
            {},
            f"Queries on {i.table.qualified} cannot rely on {i.name} until every partition has "
            f"its index. Create the missing ones on each partition (CREATE INDEX CONCURRENTLY "
            f"works there) and attach them with ALTER INDEX {i.name} ATTACH PARTITION ...; or "
            f"drop {i.name} if it is no longer wanted.",
        )
    return _observed(
        i,
        "invalid_index",
        "medium",
        f"{i.name} is invalid: maintained on every write, never used ({_size(i)})",
        {},
        "A failed or interrupted CREATE INDEX CONCURRENTLY leaves an index behind that every "
        "write updates but no query can use. Drop it, then rebuild it with CREATE INDEX "
        "CONCURRENTLY if it is still wanted. If a concurrent build is running right now, wait "
        "for it to finish first.",
    )


def _duplicate(i: IndexStats, keep: IndexStats) -> Observed:
    why = " (it backs a constraint)" if keep.constraint else ""
    drop = (
        f"Drop the constraint {i.name} enforces (ALTER TABLE {i.table.qualified} DROP "
        "CONSTRAINT ...), which drops the index with it,"
        if i.constraint
        else f"Drop {i.name}"
    )
    return _observed(
        i,
        "duplicate_index",
        "medium",
        f"{i.name} duplicates {keep.name} ({_size(i)})",
        {"kind": "duplicate", "duplicate_of": keep.name},
        f"{i.name} repeats {keep.name} exactly, so every write updates both. "
        f"{drop} and keep {keep.name}{why}.",
    )


def _overlapping(i: IndexStats, wider: IndexStats) -> Observed:
    return _observed(
        i,
        "duplicate_index",
        "low",
        f"{i.name} is a prefix of {wider.name} ({_size(i)})",
        {"kind": "overlapping", "duplicate_of": wider.name},
        f"{wider.name} starts with the same columns, so it serves the queries {i.name} does, "
        f"slightly less compactly. Drop {i.name} unless a hot query depends on its smaller size.",
    )


def _unused(
    i: IndexStats, stats_reset: datetime | None, window: int | None, on_replica: bool
) -> Observed:
    since = (
        "since statistics began"
        if window is None
        else f"in {window} days since statistics were reset"
    )
    advice = [
        "Before dropping it, check every replica (each counts its own scans) and any job that "
        "runs less often than the statistics window."
    ]
    if window is not None and window < SHORT_STATS_WINDOW_DAYS:
        advice.append(
            f"Statistics were reset only {window} days ago: wait for a longer window first."
        )
    if on_replica:
        advice.append("These scans were counted on a replica: check the primary as well.")
    return _observed(
        i,
        "unused_index",
        "medium" if i.index_bytes >= UNUSED_LARGE_BYTES else "low",
        f"{i.name} has not been scanned {since} ({_size(i)})",
        {
            "stats_reset": None if stats_reset is None else stats_reset.isoformat(),
            "stats_age_days": window,
            "counted_on": "replica" if on_replica else "primary",
        },
        " ".join(advice),
    )


def _observed(
    i: IndexStats,
    category: FindingCategory,
    severity: Severity,
    title: str,
    evidence: dict[str, Any],
    recommendation: str,
) -> Observed:
    return Observed(
        category=category,
        subject=i.name,
        severity=severity,
        title=title,
        evidence={
            "table": i.table.qualified,
            "method": i.method,
            "columns": [c if c is not None else "(expression)" for c in i.columns],
            "partial": i.predicate is not None,
            "index_bytes": i.index_bytes,
            "scans": i.scans,
            **evidence,
        },
        recommendation=recommendation,
        ddl=_drop(i),
        collection=i.table.qualified,
    )


def _drop(i: IndexStats) -> str | None:
    """Ready to copy, or None when the index belongs to a constraint and goes with it. A
    partitioned index cannot be dropped concurrently: DROP INDEX locks the table."""
    if i.constraint:
        return None
    return f"DROP INDEX {i.name};" if i.partitioned else f"DROP INDEX CONCURRENTLY {i.name};"


def _size(i: IndexStats) -> str:
    return format_bytes(i.index_bytes)


def _days(at: datetime | None, now: datetime) -> int | None:
    return None if at is None else (now - at).days
