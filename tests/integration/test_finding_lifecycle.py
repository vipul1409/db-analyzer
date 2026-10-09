import json
from collections.abc import Iterator

import psycopg
import pytest

from db_analyzer.core.model import Finding, FindingStatus
from db_analyzer.service import AnalyzerService
from tests.fixtures.dataset import GROUND_TRUTH, fixture_dsn

from .conftest import Shop

pytestmark = pytest.mark.integration

SCRATCH = "public.lifecycle_scratch"
INDEX_HEAVY = f"size:{SCRATCH}:index_heavy"
BLOAT = "bloat:public.audit_log"


def as_postgres(shop: Shop, sql: str) -> None:
    dsn = fixture_dsn(shop.major, "postgres", GROUND_TRUTH["database"])
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql)


@pytest.fixture
def scratch(shop: Shop) -> Iterator[str]:
    """A table with more index than heap, so it has an index_heavy Finding; dropped after."""
    as_postgres(
        shop,
        f"""DROP TABLE IF EXISTS {SCRATCH};
            CREATE TABLE {SCRATCH} AS
              SELECT g AS id, g * 7 AS a, g * 13 AS b FROM generate_series(1, 40000) g;
            CREATE INDEX lifecycle_scratch_id ON {SCRATCH} (id);
            CREATE INDEX lifecycle_scratch_a ON {SCRATCH} (a);
            CREATE INDEX lifecycle_scratch_b ON {SCRATCH} (b);
            ANALYZE {SCRATCH};""",
    )
    yield SCRATCH
    as_postgres(shop, f"DROP TABLE IF EXISTS {SCRATCH}")


def finding(service: AnalyzerService, shop: Shop, fingerprint: str) -> Finding:
    statuses: tuple[FindingStatus, ...] = ("open", "acknowledged", "fixed", "obsolete")
    [f] = [f for f in service.findings(shop.id, statuses) if f.fingerprint == fingerprint]
    return f


def test_an_acknowledged_finding_stays_acknowledged_when_observed_again(
    service: AnalyzerService,
    shop: Shop,
) -> None:
    service.run(shop.id, ["inventory"])
    service.set_finding_status(shop.id, BLOAT, "acknowledged")

    second = service.run(shop.id, ["inventory"])

    f = finding(service, shop, BLOAT)
    assert (f.status, f.last_seen_run, f.unobserved_by) == ("acknowledged", second.id, None)
    assert len(service.observations(shop.id, BLOAT)) == 2


def test_a_covered_finding_no_longer_observed_asks_fixed_and_is_never_auto_fixed(
    service: AnalyzerService,
    shop: Shop,
    scratch: str,
) -> None:
    first = service.run(shop.id, ["inventory"])
    assert finding(service, shop, INDEX_HEAVY).status == "open"
    as_postgres(shop, "DROP INDEX lifecycle_scratch_a, lifecycle_scratch_b")

    second = service.run(shop.id, ["inventory"])

    f = finding(service, shop, INDEX_HEAVY)
    assert (f.status, f.unobserved_by, f.last_seen_run) == ("open", second.id, first.id)

    service.run(shop.id, ["inventory"], collections=["public.tenants"])
    assert finding(service, shop, INDEX_HEAVY).unobserved_by == second.id, "not covered"

    fixed = service.set_finding_status(shop.id, INDEX_HEAVY, "fixed")
    assert (fixed.status, fixed.unobserved_by) == ("fixed", None)


def test_size_rankings_never_ask_fixed(
    service: AnalyzerService,
    shop: Shop,
) -> None:
    service.run(shop.id, ["inventory"])

    service.run(shop.id, ["inventory"], collections=["public.events"])  # ranks nothing

    largest = finding(service, shop, f"size:{GROUND_TRUTH['inventory']['largest_table']}")
    assert (largest.status, largest.unobserved_by) == ("open", None)


def test_a_dropped_table_makes_its_findings_obsolete_and_hidden_by_default(
    service: AnalyzerService,
    shop: Shop,
    scratch: str,
) -> None:
    service.run(shop.id, ["inventory"])
    as_postgres(shop, f"DROP TABLE {scratch}")

    service.run(shop.id, ["inventory"])

    assert INDEX_HEAVY not in {f.fingerprint for f in service.findings(shop.id)}
    obsolete = service.findings(shop.id, ["obsolete"])
    assert INDEX_HEAVY in {f.fingerprint for f in obsolete}
    assert finding(service, shop, INDEX_HEAVY).unobserved_by is None


def test_comparing_runs_reports_differences_over_shared_scope_only(
    service: AnalyzerService,
    shop: Shop,
    scratch: str,
) -> None:
    broad = service.run(shop.id, ["inventory"])
    as_postgres(shop, "DROP INDEX lifecycle_scratch_a, lifecycle_scratch_b")
    targeted = service.run(shop.id, ["inventory"], collections=[scratch, "public.tenants"])

    c = service.compare_runs(targeted.id, broad.id)

    assert (c.before, c.after) == (broad.id, targeted.id)
    assert c.shared == {"inventory": ["public.lifecycle_scratch", "public.tenants"]}
    assert "public.events" in c.not_compared["inventory"]
    assert {s.collection for s in c.size_changes} == {SCRATCH, "public.tenants"}
    assert c.size_changes[0].collection == SCRATCH and c.size_changes[0].delta_bytes < 0
    assert c.disappeared == [INDEX_HEAVY]
    assert BLOAT not in c.disappeared, "audit_log is outside the targeted Run"


def test_json_exports_of_two_runs_diff_cleanly(
    service: AnalyzerService,
    shop: Shop,
) -> None:
    first = service.run(shop.id, ["inventory"])
    second = service.run(shop.id, ["inventory"])

    a, b = service.export(first.id, "json").decode(), service.export(second.id, "json").decode()

    doc = json.loads(a)
    assert doc["run"]["id"] == first.id and doc["connection"] == shop.connection.name
    assert {c["collection"] for c in doc["collections"]} >= {"public.events", "public.tenants"}
    assert any(f["fingerprint"] == BLOAT for f in doc["findings"])
    keys = [[line.split(":")[0] for line in text.splitlines()] for text in (a, b)]
    assert keys[0] == keys[1], "same layout line by line: a diff shows only changed values"
