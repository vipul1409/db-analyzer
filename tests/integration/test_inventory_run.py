import re
from typing import Any

import psycopg
import pytest

from db_analyzer.analyzers import inventory
from db_analyzer.core.model import GateLimits, StorageStats, UnknownCollections
from db_analyzer.service import AnalyzerService
from tests.fixtures.dataset import GROUND_TRUTH, fixture_dsn

from .conftest import Shop

pytestmark = pytest.mark.integration

SHOP_TABLES = {
    "public.tenants",
    "public.accounts",
    "public.bookings",
    "public.booking_items",
    "public.events",
    "public.audit_log",
    "public.legacy_imports",
    "public.usage_records",
    "reference.sku_categories",
}
STALE = "public.legacy_imports"  # ground truth: statistics deliberately out of date
INVENTORY_PROBLEMS = {  # ground-truth Findings the inventory analyzer is responsible for
    f["fingerprint"]: f
    for f in GROUND_TRUTH["findings"]
    if f["fingerprint"].split(":")[0] in ("bloat", "stale_stats", "size")
}


def truth(shop: Shop) -> dict[str, dict[str, Any]]:
    """Sizes and exact row counts read directly as the superuser, partitions summed. Heap is
    built from the fork sizes, independently of the template's total - index - toast."""
    with psycopg.connect(fixture_dsn(shop.major, "postgres", GROUND_TRUTH["database"])) as conn:
        rows = conn.execute(
            """SELECT n.nspname || '.' || c.relname,
                      sum(pg_relation_size(p.oid, 'main') + pg_relation_size(p.oid, 'fsm')
                          + pg_relation_size(p.oid, 'vm')),
                      sum(pg_indexes_size(p.oid)),
                      sum(coalesce(pg_relation_size(p.reltoastrelid)
                                   + pg_indexes_size(p.reltoastrelid), 0)),
                      sum(pg_total_relation_size(p.oid))
               FROM pg_class c
               LEFT JOIN LATERAL pg_partition_tree(c.oid) t ON c.relkind = 'p' AND t.isleaf
               JOIN pg_class p ON p.oid = coalesce(t.relid, c.oid)
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname IN ('public', 'reference') AND c.relkind IN ('r', 'p')
                 AND NOT c.relispartition
               GROUP BY n.nspname, c.relname"""
        ).fetchall()
        return {
            name: {
                "heap_bytes": int(heap),
                "index_bytes": int(index),
                "toast_bytes": int(toast),
                "total_bytes": int(total),
                "rows": conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0],  # type: ignore[index]
            }
            for name, heap, index, toast, total in rows
        }


def measured(service: AnalyzerService, run_id: str) -> dict[str, StorageStats]:
    return {s.ref.qualified: s for s in service.storage(run_id)}


def test_inventory_run_records_scope(service: AnalyzerService, shop: Shop) -> None:
    run = service.run(shop.id, analyzers=["inventory"])

    assert run.status == "complete"
    assert run.finished_at is not None
    assert {r.qualified for r in run.scope["inventory"]} == SHOP_TABLES
    assert run.skipped == {}
    assert [r.id for r in service.runs(shop.id)] == [run.id]


def test_sizes_and_row_estimates_match_ground_truth(service: AnalyzerService, shop: Shop) -> None:
    run = service.run(shop.id, analyzers=["inventory"])
    sizes = measured(service, run.id)
    expected = truth(shop)

    assert set(sizes) == SHOP_TABLES
    largest = max(sizes.values(), key=lambda s: s.total_bytes)
    assert largest.ref.qualified == GROUND_TRUTH["inventory"]["largest_table"]
    assert sizes["public.usage_records"].ref.kind == "partitioned_table"
    assert sizes["public.events"].toast_bytes is not None
    for name, s in sizes.items():
        assert s.data_bytes + s.index_bytes + (s.toast_bytes or 0) == s.total_bytes, name
        truth_of = expected[name]
        assert s.total_bytes == pytest.approx(truth_of["total_bytes"], rel=0.02), name
        assert s.data_bytes == pytest.approx(truth_of["heap_bytes"], rel=0.02), name
        assert s.index_bytes == pytest.approx(truth_of["index_bytes"], rel=0.02), name
        assert s.toast_bytes == pytest.approx(truth_of["toast_bytes"], rel=0.02), name
        assert s.row_count_method == "estimate"
        if name != STALE:
            assert s.row_count == pytest.approx(expected[name]["rows"], rel=0.1), name


def test_size_findings_are_recorded_with_an_observation(
    service: AnalyzerService, shop: Shop
) -> None:
    run = service.run(shop.id, analyzers=["inventory"])

    fingerprints = {f.fingerprint for f in service.findings(shop.id)}
    largest = f"size:{GROUND_TRUTH['inventory']['largest_table']}"
    assert largest in fingerprints
    [observation] = service.observations(shop.id, largest)
    assert observation.run_id == run.id
    assert observation.evidence["rank"] == 1


def test_second_run_re_observes_the_same_findings(service: AnalyzerService, shop: Shop) -> None:
    first = service.run(shop.id, analyzers=["inventory"])
    second = service.run(shop.id, analyzers=["inventory"])

    findings = service.findings(shop.id)
    assert len(findings) == len({f.fingerprint for f in findings})
    largest = f"size:{GROUND_TRUTH['inventory']['largest_table']}"
    assert [o.run_id for o in service.observations(shop.id, largest)] == [first.id, second.id]


def test_markdown_report_lists_tables_by_size(service: AnalyzerService, shop: Shop) -> None:
    run = service.run(shop.id, analyzers=["inventory"])

    report = service.export(run.id, "md").decode()

    assert report.startswith("# Inventory report: ")
    rows = [line for line in report.splitlines() if re.match(r"\| \d+ \| \w+\.", line)]
    assert rows[0].startswith("| 1 | public.events |")
    assert len(rows) >= len(SHOP_TABLES)
    assert "(estimate)" in report
    assert "| public.usage_records | partitioned table (20 partitions) |" in report.replace(
        f"({tenants(shop)} partitions)", "(20 partitions)"
    )
    schemas = report.split("## Schemas")[1].split("##")[0]
    assert "| public | 8 |" in schemas and "| reference | 1 |" in schemas
    findings = report.split("## Findings")[1].split("##")[0].strip().splitlines()
    assert findings[0].startswith("1. **high** `bloat:public.audit_log`")
    assert "Autovacuum is disabled" in report  # recommendations are shown


def test_markdown_report_flags_exact_counts_and_skipped_ones(
    service: AnalyzerService, shop: Shop
) -> None:
    service.add_connection(
        shop.connection.name, "DBX_TEST_SHOP_DSN", gate=GateLimits(max_scan_rows=1_000)
    )
    run = service.run(
        shop.id, ["inventory"], collections=["public.tenants", "public.events"], exact_counts=True
    )

    report = service.export(run.id, "md").decode()

    assert re.search(r"\| public\.tenants \|.*\| 20 \(exact\) \|", report)
    assert "- public.events: exact count skipped: scan rows" in report


def test_sizes_are_read_from_the_catalog_so_the_gate_never_blocks_them(
    service: AnalyzerService, shop: Shop
) -> None:
    tiny = GateLimits(max_total_cost=1, max_result_rows=1, max_scan_rows=1)
    service.add_connection(shop.connection.name, "DBX_TEST_SHOP_DSN", gate=tiny)

    run = service.run(shop.id, analyzers=["inventory"])

    assert run.status == "complete"
    assert set(measured(service, run.id)) == SHOP_TABLES
    audit = service.audit(shop.id)
    assert all(a.plan_cost is None for a in audit if a.purpose == "inventory")
    # Only the dead-tuple scan reads table data; the gate keeps it out, with the reason.
    log = measured(service, run.id)["public.audit_log"]
    assert log.dead_tuple_scan is None
    assert "scan limit" in log.skipped["dead_tuple_scan"]


def test_partitions_roll_up_into_their_parent(service: AnalyzerService, shop: Shop) -> None:
    run = service.run(shop.id, analyzers=["inventory"])
    sizes = measured(service, run.id)

    usage = sizes["public.usage_records"]
    assert usage.partitions == tenants(shop)  # ground truth: one partition per tenant
    assert all(s.partitions is None for name, s in sizes.items() if name != "public.usage_records")
    assert not any("usage_records_t" in name for name in sizes), "leaves are not listed alone"


def test_schema_rollup_matches_ground_truth(service: AnalyzerService, shop: Shop) -> None:
    run = service.run(shop.id, analyzers=["inventory"])
    expected = truth(shop)

    rollup = {t.schema: t for t in inventory.schema_rollup(service.storage(run.id))}

    assert {s: t.collections for s, t in rollup.items()} == GROUND_TRUTH["inventory"]["schemas"]
    for schema, t in rollup.items():
        in_schema = [v for name, v in expected.items() if name.startswith(f"{schema}.")]
        assert t.total_bytes == pytest.approx(sum(v["total_bytes"] for v in in_schema), rel=0.02)
        assert t.index_bytes == pytest.approx(sum(v["index_bytes"] for v in in_schema), rel=0.02)


def test_vacuum_and_analyze_recency_are_measured(service: AnalyzerService, shop: Shop) -> None:
    run = service.run(shop.id, analyzers=["inventory"])
    sizes = measured(service, run.id)

    for name, s in sizes.items():
        assert s.maintenance is not None, name
        assert s.maintenance.last_analyze is not None, name  # the seed analyzes every table
    assert sizes["public.audit_log"].maintenance.autovacuum_disabled  # type: ignore[union-attr]
    assert not sizes["public.events"].maintenance.autovacuum_disabled  # type: ignore[union-attr]


def test_seeded_problems_become_exactly_the_expected_findings(
    service: AnalyzerService, shop: Shop
) -> None:
    run = service.run(shop.id, analyzers=["inventory"])

    problems = {o.fingerprint: o for o in service.run_observations(run.id) if o.severity != "info"}

    assert set(problems) == set(INVENTORY_PROBLEMS)
    bloat = problems["bloat:public.audit_log"]
    assert bloat.evidence["measured_by"] == "pgstattuple_approx"
    min_wasted = INVENTORY_PROBLEMS["bloat:public.audit_log"]["min_wasted_percent"]
    assert bloat.evidence["wasted_percent"] >= min_wasted
    assert bloat.evidence["autovacuum_disabled"] is True
    stale = problems[f"stale_stats:{STALE}"]
    max_ratio = INVENTORY_PROBLEMS[f"stale_stats:{STALE}"]["max_reltuples_ratio"]
    assert stale.evidence["estimated_rows"] <= max_ratio * stale.evidence["live_rows"]


def test_exact_counts_run_where_the_gate_allows(service: AnalyzerService, shop: Shop) -> None:
    asked = ["public.tenants", "legacy_imports"]  # unqualified names resolve when unique

    run = service.run(shop.id, analyzers=["inventory"], collections=asked, exact_counts=True)

    sizes = measured(service, run.id)
    assert set(sizes) == {"public.tenants", STALE}
    assert {r.qualified for r in run.scope["inventory"]} == {"public.tenants", STALE}
    expected = truth(shop)
    for name, s in sizes.items():
        assert (s.row_count_method, s.row_count) == ("exact", expected[name]["rows"])
        assert "exact_count" not in s.skipped
    assert not [o for o in service.run_observations(run.id) if o.severity == "info"], (
        "a targeted Run ranks no sizes"
    )


def test_exact_counts_the_gate_refuses_are_skipped_with_the_reason(
    service: AnalyzerService, shop: Shop
) -> None:
    gate = GateLimits(max_scan_rows=1_000)  # tenants (20 rows) passes, events does not
    service.add_connection(shop.connection.name, "DBX_TEST_SHOP_DSN", gate=gate)

    run = service.run(
        shop.id, ["inventory"], collections=["public.tenants", "public.events"], exact_counts=True
    )

    sizes = measured(service, run.id)
    assert sizes["public.tenants"].row_count_method == "exact"
    events = sizes["public.events"]
    assert events.row_count_method == "estimate"
    assert "scan rows" in events.skipped["exact_count"]
    assert run.status == "complete"  # the table was measured; only its count was skipped


def test_unknown_collections_are_refused_by_name(service: AnalyzerService, shop: Shop) -> None:
    with pytest.raises(UnknownCollections) as e:
        service.run(shop.id, ["inventory"], collections=["public.nope"], exact_counts=True)

    assert e.value.names == ["public.nope"]


def tenants(shop: Shop) -> int:
    with psycopg.connect(fixture_dsn(shop.major, "postgres", GROUND_TRUTH["database"])) as conn:
        return int(conn.execute("SELECT count(*) FROM tenants").fetchone()[0])  # type: ignore[index]
