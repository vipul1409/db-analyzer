from typing import Any, NamedTuple

import psycopg
import pytest

from db_analyzer.core.model import Connection, GateLimits, GateRejected, StorageStats
from db_analyzer.service import AnalyzerService
from tests.fixtures.dataset import GROUND_TRUTH, fixture_dsn

from .conftest import SUPPORTED, seeded_dsn

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
}
STALE = "public.legacy_imports"  # ground truth: statistics deliberately out of date


class Shop(NamedTuple):
    connection: Connection
    major: int

    @property
    def id(self) -> str:
        return self.connection.id


@pytest.fixture(params=SUPPORTED)
def shop(
    request: pytest.FixtureRequest, service: AnalyzerService, monkeypatch: pytest.MonkeyPatch
) -> Shop:
    major = request.param
    monkeypatch.setenv("DBX_TEST_SHOP_DSN", seeded_dsn(major))
    return Shop(service.add_connection(f"shop{major}", dsn_env="DBX_TEST_SHOP_DSN"), major)


def truth(shop: Shop) -> dict[str, dict[str, Any]]:
    """Sizes and exact row counts read directly as the superuser, partitions summed. Heap is
    built from the fork sizes, independently of the template's total - index - toast."""
    with psycopg.connect(fixture_dsn(shop.major, "postgres", GROUND_TRUTH["database"])) as conn:
        rows = conn.execute(
            """SELECT 'public.' || c.relname,
                      sum(pg_relation_size(p.oid, 'main') + pg_relation_size(p.oid, 'fsm')
                          + pg_relation_size(p.oid, 'vm')),
                      sum(pg_indexes_size(p.oid)),
                      sum(coalesce(pg_relation_size(p.reltoastrelid)
                                   + pg_indexes_size(p.reltoastrelid), 0)),
                      sum(pg_total_relation_size(p.oid))
               FROM pg_class c
               LEFT JOIN LATERAL pg_partition_tree(c.oid) t ON c.relkind = 'p' AND t.isleaf
               JOIN pg_class p ON p.oid = coalesce(t.relid, c.oid)
               WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p')
                 AND NOT c.relispartition
               GROUP BY c.relname"""
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
    rows = [line for line in report.splitlines() if line.startswith("| ") and "public." in line]
    assert rows[0].startswith("| 1 | public.events |")
    assert len(rows) >= len(SHOP_TABLES)
    assert "(estimate)" in report


def test_run_over_gate_limits_fails_with_audited_reason(
    service: AnalyzerService, shop: Shop
) -> None:
    service.add_connection(
        shop.connection.name, "DBX_TEST_SHOP_DSN", gate=GateLimits(max_total_cost=1)
    )

    with pytest.raises(GateRejected) as e:
        service.run(shop.id, analyzers=["inventory"])

    assert e.value.metric == "total_cost"
    [run] = service.runs(shop.id)
    assert run.status == "failed"
    rejected = [a for a in service.audit(shop.id) if a.decision == "rejected"]
    assert rejected and rejected[0].reason == e.value.reason
