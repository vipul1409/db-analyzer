from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from db_analyzer.adapters.postgres import workload as pg_workload
from db_analyzer.adapters.postgres.session import open_session
from db_analyzer.analyzers import workload
from db_analyzer.core.model import Connection, Observation, SessionLimits
from db_analyzer.safety.executor import SafeExecutor
from db_analyzer.service import AnalyzerService
from tests.fixtures.dataset import GROUND_TRUTH, fixture_dsn

from .conftest import SUPPORTED, Shop, seeded_dsn

pytestmark = pytest.mark.integration

SLOW = GROUND_TRUTH["slow_queries"]
# The seed reset the statistics minutes ago: let the Run rank them anyway.
ANY_WINDOW = timedelta(0)


def slow_queries(observations: list[Observation]) -> list[Observation]:
    return [o for o in observations if o.fingerprint.startswith("slow_query:")]


def test_seeded_slow_statements_are_ranked(service: AnalyzerService, shop: Shop) -> None:
    run = service.run(shop.id, ["workload"], min_stats_window=ANY_WINDOW)

    ranked = slow_queries(service.run_observations(run.id))

    assert run.status == "complete"
    texts = [o.evidence["text"] for o in ranked]
    for q in SLOW:
        assert any(q["match"] in t for t in texts), q["match"]
    by_text = {o.evidence["text"]: o for o in ranked}
    [sort] = [o for t, o in by_text.items() if "FROM events ORDER BY k" in t]
    assert sort.evidence["temp_blks_written"] > 0
    assert "temp_blocks_written" in sort.evidence["ranked_by"]


def test_the_analyzers_own_statements_are_not_ranked(service: AnalyzerService, shop: Shop) -> None:
    service.probe(shop.id)  # its statements are in pg_stat_statements now, under our role
    with psycopg.connect(fixture_dsn(shop.major, "postgres", GROUND_TRUTH["database"])) as conn:
        [recorded] = conn.execute(
            """SELECT count(*) FROM pg_stat_statements
               WHERE query LIKE '%%FROM pg_extension%%' AND userid = 'db_analyzer'::regrole"""
        ).fetchone() or [0]
    assert recorded, "pg_stat_statements.track = all records the analyzer's own statements"

    run = service.run(shop.id, ["workload"], min_stats_window=ANY_WINDOW)

    report = service.workload(run.id)
    assert report is not None
    assert [i.text for i in report.items if "pg_extension" in i.text] == []


def test_a_recent_stats_reset_warns_and_ranks_nothing(service: AnalyzerService, shop: Shop) -> None:
    run = service.run(shop.id, ["workload"], min_stats_window=timedelta(days=3650))

    report = service.workload(run.id)

    assert report is not None
    assert report.refused is not None and "reset" in report.refused
    assert report.items == []
    assert report.warnings[0] == report.refused
    assert slow_queries(service.run_observations(run.id)) == []
    assert run.status == "partial"
    assert "workload" not in run.scope


def test_workload_and_inventory_can_share_a_run(service: AnalyzerService, shop: Shop) -> None:
    run = service.run(shop.id, ["inventory", "workload"], min_stats_window=ANY_WINDOW)

    found = {o.fingerprint.split(":")[0] for o in service.run_observations(run.id)}

    assert {"slow_query", "size", "bloat"} <= found
    assert set(run.scope) == {"inventory", "workload"}
    assert service.storage(run.id)


def test_a_workload_run_report_lists_the_statements(
    service: AnalyzerService, shop: Shop, tmp_path: Path
) -> None:
    run = service.run(shop.id, ["workload"], min_stats_window=ANY_WINDOW)

    md = service.export(run.id, "md").decode()
    js = service.export(run.id, "json").decode()

    assert md.startswith("# Workload report")
    assert "## Workload" in md and "FROM bookings WHERE account_id = $1" in md
    assert '"workload"' in js and '"slow_query:' in js


def test_the_same_slow_query_finding_persists_across_runs(
    service: AnalyzerService, shop: Shop
) -> None:
    first = service.run(shop.id, ["workload"], min_stats_window=ANY_WINDOW)
    second = service.run(shop.id, ["workload"], min_stats_window=ANY_WINDOW)

    ids = {o.fingerprint for o in slow_queries(service.run_observations(first.id))}

    assert ids == {o.fingerprint for o in slow_queries(service.run_observations(second.id))}
    assert {f.fingerprint for f in service.findings(shop.connection.id)} >= ids


@pytest.mark.skipif(not {15, 16} <= set(SUPPORTED), reason="needs PG_VERSIONS to include 15 and 16")
def test_the_same_statement_has_the_same_fingerprint_on_pg15_and_pg16() -> None:
    def fingerprints(major: int) -> dict[str, str]:
        with open_session(seeded_dsn(major), SessionLimits()) as conn:
            executor = SafeExecutor(conn, "c1", audit=lambda _: None)
            from db_analyzer.adapters.postgres import probe as pg_probe

            probe = pg_probe.probe(executor)
            found = pg_workload.PG_STAT_STATEMENTS.read(executor, probe).statements
        return {
            q["match"]: workload.fingerprint(s.text)
            for q in SLOW
            for s in found
            if q["match"] in s.text
        }

    on15, on16 = fingerprints(15), fingerprints(16)

    assert set(on15) == {q["match"] for q in SLOW}
    assert on15 == on16


def test_foreign_keys_without_an_index_match_the_ground_truth(shop: Shop) -> None:
    expected = {
        f["fingerprint"]
        for f in GROUND_TRUTH["findings"]
        if f["fingerprint"].startswith("missing_index:")
    }
    with open_session(seeded_dsn(shop.major), SessionLimits()) as conn:
        executor = SafeExecutor(conn, "c1", audit=lambda _: None)
        keys = pg_workload.unindexed_foreign_keys(executor, shop.major * 10000)

    found = {o.fingerprint for o in workload.schema_only_review(keys, [])}

    assert found == expected


@pytest.fixture
def without_statements(
    shop: Shop, service: AnalyzerService, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Connection]:
    """A database with a foreign key nothing indexes and a table that is only scanned
    sequentially, in which pg_stat_statements is not installed."""
    major, name = shop.major, f"nostmts{shop.major}"
    admin = fixture_dsn(major, "postgres", "postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {name}")
    with psycopg.connect(fixture_dsn(major, "postgres", name), autocommit=True) as conn:
        conn.execute(
            """CREATE TABLE customers (id bigint PRIMARY KEY);
               CREATE TABLE orders (id bigint PRIMARY KEY,
                                    customer_id bigint NOT NULL REFERENCES customers);
               INSERT INTO customers SELECT i FROM generate_series(1, 100) i;
               INSERT INTO orders SELECT i, 1 + i % 100 FROM generate_series(1, 20000) i;
               GRANT SELECT ON ALL TABLES IN SCHEMA public TO db_analyzer"""
        )
        for _ in range(60):
            conn.execute("SELECT count(*) FROM orders WHERE customer_id = 5")
    monkeypatch.setenv("DBX_TEST_NOSTMTS_DSN", fixture_dsn(major, "db_analyzer", name))
    yield service.add_connection(name, dsn_env="DBX_TEST_NOSTMTS_DSN")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_without_pg_stat_statements_the_run_reviews_the_schema_and_says_how_to_enable_it(
    service: AnalyzerService, without_statements: Connection
) -> None:
    run = service.run(without_statements.id, ["workload"])

    report = service.workload(run.id)
    found = {o.fingerprint: o for o in service.run_observations(run.id)}

    assert report is not None and report.source is None
    assert any("CREATE EXTENSION pg_stat_statements" in s for s in report.enable_steps)
    # The fixture server preloads the module already: only the extension is missing.
    assert not any("shared_preload_libraries" in s for s in report.enable_steps)
    assert set(found) == {
        "missing_index:public.orders(customer_id)",
        "missing_index:public.orders:seq_scan_heavy",
    }
    assert found["missing_index:public.orders(customer_id)"].ddl == (
        "CREATE INDEX CONCURRENTLY ON public.orders (customer_id);"
    )
    assert run.status == "partial"
    assert [r.qualified for r in run.scope["workload"]] == ["public.customers", "public.orders"]
    assert "enable" in service.export(run.id, "md").decode().lower()
