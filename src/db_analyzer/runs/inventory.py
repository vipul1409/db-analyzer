"""The inventory analyzer in a Run: sizes, maintenance and index health of the collections, from
the catalog; dead-tuple scans and exact counts, which read table data, only where the EXPLAIN
gate allows (ADR 0007, 0009)."""

from dataclasses import replace

import psycopg

from db_analyzer.adapters.postgres import inventory as pg_inventory
from db_analyzer.analyzers import index_health, inventory
from db_analyzer.core.model import (
    GateLimits,
    ProbeResult,
    QueryCapReached,
    QueryRejected,
    StorageStats,
)
from db_analyzer.runs.base import Analyzer, Collected, RunContext
from db_analyzer.safety.executor import SafeExecutor


def measure(ctx: RunContext) -> Collected:
    """`collections` targets the Run at those tables (schema-qualified, or bare when
    unambiguous); None measures every one. `exact_counts` also counts their rows with count(*).

    Sizes come from the catalog, which needs no table privilege and passes the gate, so
    inventory skips measurements, never a whole collection. Every collection is listed before
    the scope narrows, so even a targeted Run knows which relations exist."""
    executor, probe, options = ctx.executor, ctx.probe, ctx.options
    listed = pg_inventory.storage_stats(executor, probe.server_version_num)
    indexes = pg_inventory.index_stats(executor, probe.server_version_num)
    measured = listed
    if options.collections is not None:
        measured = inventory.select(measured, list(options.collections))
    measured = _measure_table_data(executor, probe, ctx.gate, measured, options.exact_counts)
    refs = {m.ref for m in measured}
    index_problems = index_health.analyze(
        [i for i in indexes if i.table in refs],
        stats_reset=probe.stats.database_stats_reset,
        now=probe.taken_at,
        on_replica=probe.in_recovery,
    )
    return Collected(
        found=inventory.analyze(
            measured, broad=options.collections is None, other_problems=index_problems
        ),
        scope=[m.ref for m in measured],
        # Relation names share one namespace per schema (ADR 0009).
        existing={s.ref.qualified for s in listed} | {i.name for i in indexes},
        storage=measured,
    )


ANALYZER = Analyzer(
    "inventory",
    options=frozenset({"collections", "exact_counts"}),
    covers=frozenset(
        {"size", "bloat", "stale_stats", "unused_index", "duplicate_index", "invalid_index"}
    ),
    measure=measure,
)


def _measure_table_data(
    executor: SafeExecutor,
    probe: ProbeResult,
    gate: GateLimits,
    measured: list[StorageStats],
    exact_counts: bool,
) -> list[StorageStats]:
    """The measurements that read table data rather than the catalog: a dead-tuple scan where
    the counters suggest bloat, and exact counts when asked. Each is skipped, with the reason,
    where the gate or a privilege refuses it; the query cap skips everything after it."""
    out: dict[int, StorageStats] = {i: s for i, s in enumerate(measured)}
    pgstattuple = probe.extension_schemas.get("pgstattuple")
    jobs: list[tuple[int, str]] = [
        (i, "dead_tuple_scan") for i, s in out.items() if inventory.needs_dead_tuple_scan(s)
    ]
    if exact_counts:  # smallest first, so a cap or timeout costs the fewest counts
        jobs += [(i, "exact_count") for i in sorted(out, key=lambda i: out[i].total_bytes)]

    def skip(i: int, job: str, why: str) -> None:
        out[i] = replace(out[i], skipped={**out[i].skipped, job: why})

    capped: str | None = None
    for i, job in jobs:
        s = out[i]
        if capped is not None:
            skip(i, job, capped)
            continue
        try:
            if job == "exact_count":
                n = pg_inventory.count_exactly(executor, s.ref)
                out[i] = replace(s, row_count=n, row_count_method="exact")
            elif pgstattuple is None:
                skip(i, job, "pgstattuple is not installed")
            elif (rows := _rows_to_read(s)) > gate.max_scan_rows:
                skip(
                    i,
                    job,
                    f"about {rows:,} rows to read exceeds the scan limit ({gate.max_scan_rows:,})",
                )
            else:
                out[i] = replace(
                    s, dead_tuple_scan=pg_inventory.scan_dead_tuples(executor, s.ref, pgstattuple)
                )
        except QueryCapReached as e:
            capped = e.reason
            skip(i, job, capped)
        except QueryRejected as e:
            skip(i, job, e.reason)
        except psycopg.errors.InsufficientPrivilege:
            skip(i, job, "no SELECT privilege on the table")
        except psycopg.Error as e:
            skip(i, job, str(e).strip())
    return list(out.values())


def _rows_to_read(s: StorageStats) -> int:
    m = s.maintenance
    return 0 if m is None else m.live_rows + m.dead_rows
