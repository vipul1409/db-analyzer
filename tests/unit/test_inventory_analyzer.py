from dataclasses import replace
from datetime import UTC, datetime

from db_analyzer.analyzers import inventory
from db_analyzer.core.model import (
    CollectionKind,
    CollectionRef,
    DeadTupleScan,
    Maintenance,
    Observed,
    StorageStats,
)

MB = 1024 * 1024
LAST_WEEK = datetime(2026, 10, 2, tzinfo=UTC)


def stats(name: str, total: int, rows: int | None = 10, schema: str = "public") -> StorageStats:
    return StorageStats(
        ref=CollectionRef(namespace=schema, name=name, kind=CollectionKind.TABLE),
        row_count=rows,
        row_count_method="estimate",
        data_bytes=total // 2,
        index_bytes=total // 4,
        toast_bytes=total - total // 2 - total // 4,
        total_bytes=total,
    )


def activity(
    live: int, dead: int = 0, modified: int = 0, analyzed: datetime | None = LAST_WEEK
) -> Maintenance:
    return Maintenance(
        live_rows=live,
        dead_rows=dead,
        modified_since_analyze=modified,
        last_vacuum=None,
        last_analyze=analyzed,
        autovacuum_disabled=False,
    )


def problems(measured: list[StorageStats]) -> dict[str, Observed]:
    return {o.fingerprint: o for o in inventory.analyze(measured) if o.severity != "info"}


def test_largest_collections_become_size_findings_in_rank_order() -> None:
    measured = [stats(f"t{i}", total=i * 1_000_000) for i in range(1, 9)]

    found = inventory.analyze(measured, top_n=3)

    assert [f.fingerprint for f in found] == ["size:public.t8", "size:public.t7", "size:public.t6"]
    assert {f.category for f in found} == {"size"}
    assert {f.severity for f in found} == {"info"}


def test_size_finding_carries_its_evidence() -> None:
    [top] = inventory.analyze([stats("big", 3_000_000, rows=1234), stats("small", 1_000_000)], 1)

    assert top.subject == "public.big"
    assert top.evidence == {
        "rank": 1,
        "total_bytes": 3_000_000,
        "data_bytes": 1_500_000,
        "index_bytes": 750_000,
        "toast_bytes": 750_000,
        "share_of_total": 0.75,
        "row_count": 1234,
        "row_count_method": "estimate",
    }
    assert "public.big" in top.title and "2.9 MB" in top.title


def test_empty_collections_are_not_reported() -> None:
    assert inventory.analyze([stats("empty", 0)], top_n=5) == []


def test_qualified_name_quotes_only_when_needed() -> None:
    assert CollectionRef("public", "events", CollectionKind.TABLE).qualified == "public.events"
    assert CollectionRef("App", "my table", CollectionKind.TABLE).qualified == '"App"."my table"'


def test_many_dead_tuples_per_live_row_is_bloat() -> None:
    log = replace(stats("audit_log", 8 * MB, rows=20_000), maintenance=activity(20_000, 19_000))
    fresh = replace(stats("tenants", 8 * MB, rows=20_000), maintenance=activity(20_000, 100))

    found = problems([log, fresh])

    assert list(found) == ["bloat:public.audit_log"]
    bloat = found["bloat:public.audit_log"]
    assert bloat.severity == "medium"
    assert bloat.evidence["dead_tuple_ratio"] == 0.95
    assert bloat.evidence["measured_by"] == "statistics counters"
    assert bloat.recommendation


def test_a_dead_tuple_scan_outranks_the_counters_and_names_disabled_autovacuum() -> None:
    counters = replace(activity(20_000, 0), autovacuum_disabled=True)
    scan = DeadTupleScan(live_rows=20_000, dead_rows=21_000, dead_percent=48.0, free_percent=3.0)
    log = replace(stats("audit_log", 8 * MB), maintenance=counters, dead_tuple_scan=scan)

    bloat = problems([log])["bloat:public.audit_log"]

    assert bloat.severity == "high"
    assert bloat.evidence["dead_tuple_ratio"] == 1.05
    assert bloat.evidence["measured_by"] == "pgstattuple_approx"
    assert bloat.evidence["dead_percent"] == 48.0
    assert bloat.evidence["autovacuum_disabled"] is True
    assert "autovacuum" in (bloat.recommendation or "")


def test_rows_changed_since_analyze_far_beyond_the_estimate_is_stale_stats() -> None:
    legacy = replace(
        stats("legacy_imports", 4 * MB, rows=100), maintenance=activity(50_000, 0, 49_900)
    )
    busy = replace(stats("bookings", 4 * MB, rows=20_000), maintenance=activity(20_000, 0, 500))

    found = problems([legacy, busy])

    assert list(found) == ["stale_stats:public.legacy_imports"]
    stale = found["stale_stats:public.legacy_imports"]
    assert stale.severity == "medium"  # the estimate is off by more than 10x
    assert stale.evidence["estimated_rows"] == 100
    assert stale.evidence["live_rows"] == 50_000
    assert stale.evidence["modified_since_analyze"] == 49_900
    assert stale.evidence["last_analyze"] == LAST_WEEK.isoformat()
    assert "ANALYZE public.legacy_imports" in (stale.recommendation or "")


def test_a_large_table_never_analyzed_has_stale_stats() -> None:
    never = replace(stats("imports", 4 * MB, rows=None), maintenance=activity(5_000, analyzed=None))
    tiny = replace(stats("flags", 1 * MB, rows=None), maintenance=activity(30, analyzed=None))

    found = problems([never, tiny])

    assert list(found) == ["stale_stats:public.imports"]
    assert found["stale_stats:public.imports"].evidence["last_analyze"] is None


def sized(name: str, heap: int, index: int, toast: int = 0) -> StorageStats:
    return StorageStats(
        ref=CollectionRef("public", name, CollectionKind.TABLE),
        row_count=10,
        row_count_method="estimate",
        data_bytes=heap,
        index_bytes=index,
        toast_bytes=toast,
        total_bytes=heap + index + toast,
    )


def test_more_index_bytes_than_heap_is_an_index_heavy_size_finding() -> None:
    links = sized("sku_categories", heap=2 * MB, index=3 * MB)
    tiny = sized("tenants", heap=8192, index=16384)  # one heap page, two index pages: noise

    found = problems([links, tiny])

    assert list(found) == ["size:public.sku_categories:index_heavy"]
    heavy = found["size:public.sku_categories:index_heavy"]
    assert (heavy.category, heavy.subject, heavy.rule) == (
        "size",
        "public.sku_categories",
        "index_heavy",
    )
    assert heavy.severity == "low"
    assert heavy.evidence["index_to_heap_ratio"] == 1.5


def test_toast_bigger_than_the_heap_is_a_toast_oversized_size_finding() -> None:
    docs = sized("documents", heap=1 * MB, index=MB // 4, toast=6 * MB)

    found = problems([docs])

    assert list(found) == ["size:public.documents:toast_oversized"]
    assert found["size:public.documents:toast_oversized"].evidence["toast_to_heap_ratio"] == 6.0


def test_problems_come_first_ranked_by_severity_then_size() -> None:
    small_bloat = replace(stats("a", 2 * MB, rows=5_000), maintenance=activity(5_000, 6_000))
    big_bloat = replace(stats("b", 9 * MB, rows=5_000), maintenance=activity(5_000, 6_000))
    stale = replace(stats("c", 50 * MB, rows=100), maintenance=activity(50_000, 0, 49_900))
    links = sized("d", heap=2 * MB, index=3 * MB)

    found = inventory.analyze([small_bloat, big_bloat, stale, links], top_n=1)

    assert [o.fingerprint for o in found] == [
        "bloat:public.b",  # high, larger table first
        "bloat:public.a",
        "stale_stats:public.c",  # medium
        "size:public.d:index_heavy",  # low
        "size:public.c",  # facts last
    ]


def test_a_targeted_run_ranks_no_sizes() -> None:
    found = inventory.analyze(
        [stats("a", 2 * MB), sized("d", heap=2 * MB, index=3 * MB)], broad=False
    )

    assert [o.fingerprint for o in found] == ["size:public.d:index_heavy"]


def test_schema_rollup_sums_collections_per_schema_largest_first() -> None:
    measured = [
        sized("a", heap=1 * MB, index=1 * MB),
        sized("b", heap=3 * MB, index=0, toast=1 * MB),
        replace(
            sized("c", heap=10 * MB, index=2 * MB),
            ref=CollectionRef("archive", "c", CollectionKind.TABLE),
        ),
    ]

    rollup = inventory.schema_rollup(measured)

    assert rollup == [
        inventory.SchemaTotal("archive", 1, 10 * MB, 2 * MB, 0, 12 * MB, 0.6667),
        inventory.SchemaTotal("public", 2, 4 * MB, 1 * MB, 1 * MB, 6 * MB, 0.3333),
    ]


def test_free_space_left_by_pruned_dead_tuples_is_bloat_too() -> None:
    # A scan of the table pruned the dead tuples; their space stays allocated as free space.
    scan = DeadTupleScan(live_rows=20_000, dead_rows=40, dead_percent=0.1, free_percent=62.4)
    log = replace(stats("audit_log", 8 * MB), maintenance=activity(20_000, 0), dead_tuple_scan=scan)
    healthy = replace(
        stats("events", 8 * MB),
        dead_tuple_scan=DeadTupleScan(
            live_rows=20_000, dead_rows=0, dead_percent=0, free_percent=9
        ),
    )

    found = problems([log, healthy])

    assert list(found) == ["bloat:public.audit_log"]
    bloat = found["bloat:public.audit_log"]
    assert bloat.severity == "high"
    assert bloat.evidence["wasted_percent"] == 62.5
