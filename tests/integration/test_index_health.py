import psycopg
import pytest

from db_analyzer.service import AnalyzerService
from tests.fixtures.dataset import GROUND_TRUTH, fixture_dsn

from .conftest import Shop

pytestmark = pytest.mark.integration

CATEGORIES = ("unused_index", "duplicate_index", "invalid_index")
INDEX_HEALTH = {
    f["fingerprint"]: f
    for f in GROUND_TRUTH["findings"]
    if f["fingerprint"].split(":")[0] in CATEGORIES
}
UNUSED = "unused_index:public.idx_accounts_created_at"


def test_seeded_index_problems_become_exactly_the_expected_findings(
    service: AnalyzerService, shop: Shop
) -> None:
    run = service.run(shop.id, ["inventory"])

    found = {
        o.fingerprint: o
        for o in service.run_observations(run.id)
        if o.fingerprint.split(":")[0] in CATEGORIES
    }

    assert set(found) == set(INDEX_HEALTH)
    dup = found["duplicate_index:public.idx_accounts_tenant_dup"]
    expected = INDEX_HEALTH["duplicate_index:public.idx_accounts_tenant_dup"]["duplicate_of"]
    assert dup.evidence["duplicate_of"] == expected
    assert found["invalid_index:public.idx_bookings_status_unique"].ddl == (
        "DROP INDEX CONCURRENTLY public.idx_bookings_status_unique;"
    )
    [finding] = [f for f in service.findings(shop.id) if f.fingerprint == UNUSED]
    assert finding.collection == "public.accounts"


def test_primary_key_and_unique_indexes_are_never_unused(
    service: AnalyzerService, shop: Shop
) -> None:
    run = service.run(shop.id, ["inventory"])

    unused = {
        o.fingerprint.removeprefix("unused_index:")
        for o in service.run_observations(run.id)
        if o.fingerprint.startswith("unused_index:")
    }

    with psycopg.connect(fixture_dsn(shop.major, "postgres", GROUND_TRUTH["database"])) as conn:
        constrained = {
            r[0]
            for r in conn.execute(
                """SELECT n.nspname || '.' || c.relname FROM pg_index i
                   JOIN pg_class c ON c.oid = i.indexrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE (i.indisprimary OR i.indisunique)
                     AND n.nspname IN ('public', 'reference')"""
            )
        }
    assert constrained, "the seed has primary keys and unique indexes"
    assert not unused & constrained


def test_unused_index_findings_show_the_stats_reset_age(
    service: AnalyzerService, shop: Shop
) -> None:
    run = service.run(shop.id, ["inventory"])

    [unused] = [o for o in service.run_observations(run.id) if o.fingerprint == UNUSED]

    reset = service.probe(shop.id).stats.database_stats_reset
    assert reset is not None, "the seed resets statistics"
    assert unused.evidence["stats_reset"] == reset.isoformat()
    assert unused.evidence["stats_age_days"] is not None
    assert "since statistics were reset" in unused.title


def test_a_targeted_run_checks_only_the_indexes_of_its_tables(
    service: AnalyzerService, shop: Shop
) -> None:
    run = service.run(shop.id, ["inventory"], collections=["public.bookings"])

    found = {o.fingerprint for o in service.run_observations(run.id)}

    assert "invalid_index:public.idx_bookings_status_unique" in found
    assert "duplicate_index:public.idx_accounts_tenant_dup" not in found
