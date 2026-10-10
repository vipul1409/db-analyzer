"""Azure Query Store as a workload source, against fixture tables mimicking
azure_sys.query_store.qs_view. Live Azure verification is deferred until access exists."""

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from db_analyzer.core.model import Connection
from db_analyzer.service import AnalyzerService
from tests.fixtures.dataset import fixture_dsn

from .conftest import SUPPORTED

pytestmark = pytest.mark.integration

# Only the columns the source reads, plus the ones its filters name.
_QS_VIEW = """
CREATE SCHEMA query_store;
CREATE TABLE query_store.qs_view (
  runtime_stats_entry_id bigserial, user_id oid, db_id oid, query_id bigint,
  query_sql_text varchar(10000), plan_id bigint, start_time timestamp, end_time timestamp,
  calls bigint, total_time double precision, rows bigint, shared_blks_read bigint,
  temp_blks_written bigint, is_system_query boolean, query_type text);
GRANT USAGE ON SCHEMA query_store TO PUBLIC;
GRANT SELECT ON query_store.qs_view TO PUBLIC;
"""

LOOKUP = "SELECT * FROM orders WHERE id = 42"
BY_EMAIL = "SELECT * FROM orders WHERE customer_email = 'alice@example.com'"


@pytest.fixture(params=SUPPORTED)
def on_azure(
    request: pytest.FixtureRequest, service: AnalyzerService, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Connection]:
    """A database that looks like Azure flexible server with Query Store on and no
    pg_stat_statements, and an azure_sys database whose Query Store recorded its workload."""
    major, name = request.param, f"azureqs{request.param}"
    admin = fixture_dsn(major, "postgres", "postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        for db in (name, "azure_sys"):
            conn.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {db}")
        conn.execute(f"ALTER DATABASE {name} SET azure.extensions = ''")
        conn.execute(f"ALTER DATABASE {name} SET pg_qs.query_capture_mode = 'top'")
        row = conn.execute(
            """SELECT (SELECT oid FROM pg_database WHERE datname = %s),
                      (SELECT oid FROM pg_database WHERE datname = 'postgres'),
                      'db_writer'::regrole::oid, 'db_analyzer'::regrole::oid""",
            (name,),
        ).fetchone()
        assert row is not None
        target, other_db, app, analyzer = row
    with psycopg.connect(fixture_dsn(major, "postgres", name), autocommit=True) as conn:
        conn.execute(
            """CREATE TABLE orders (id bigint PRIMARY KEY, customer_email text);
               GRANT SELECT ON orders TO db_analyzer"""
        )
    with psycopg.connect(fixture_dsn(major, "postgres", "azure_sys"), autocommit=True) as conn:
        conn.execute(_QS_VIEW)
        rows = [
            # One statement over two intervals, summed.
            (app, target, 1, LOOKUP, "2 days", 100, 400.0, False),
            (app, target, 1, LOOKUP, "1 day", 50, 200.0, False),
            (app, target, 2, BY_EMAIL, "1 day", 10, 900.0, False),
            # Ours, another database's and Azure's own are never ranked.
            (analyzer, target, 3, "SELECT * FROM orders WHERE id = 7 AND 'ours' = 'ours'",
             "1 day", 1, 9000.0, False),
            (app, other_db, 4, "SELECT 'other database'", "1 day", 1, 9000.0, False),
            (10, target, 5, "SELECT 'azure itself'", "1 day", 1, 9000.0, True),
        ]  # fmt: skip
        for user, db, query_id, text, ago, calls, total, system in rows:
            conn.execute(
                """INSERT INTO query_store.qs_view (user_id, db_id, query_id, query_sql_text,
                     start_time, end_time, calls, total_time, rows, shared_blks_read,
                     temp_blks_written, is_system_query, query_type)
                   VALUES (%s, %s, %s, %s, (now() AT TIME ZONE 'utc') - %s::interval,
                     (now() AT TIME ZONE 'utc') - %s::interval + interval '15 minutes',
                     %s, %s, %s, 10, 0, %s, 'select')""",
                (user, db, query_id, text, ago, ago, calls, total, calls, system),
            )
    monkeypatch.setenv("DBX_TEST_AZURE_DSN", fixture_dsn(major, "db_analyzer", name))
    yield service.add_connection(name, dsn_env="DBX_TEST_AZURE_DSN")
    with psycopg.connect(admin, autocommit=True) as conn:
        for db in (name, "azure_sys"):
            conn.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_without_pg_stat_statements_on_azure_the_workload_comes_from_query_store(
    service: AnalyzerService, on_azure: Connection
) -> None:
    run = service.run(on_azure.id, ["workload"])

    report = service.workload(run.id)

    assert run.status == "complete"
    assert report is not None and report.source == "azure_query_store"
    assert report.window_seconds is not None
    assert timedelta(days=2) <= timedelta(seconds=report.window_seconds) < timedelta(days=2.1)
    by_text = {i.text: i for i in report.items}
    assert set(by_text) == {
        "SELECT * FROM orders WHERE id = $1",
        "SELECT * FROM orders WHERE customer_email = $1",
    }
    lookup = by_text["SELECT * FROM orders WHERE id = $1"]
    assert (lookup.calls, lookup.total_ms) == (150, 600.0)
    found = [o for o in service.run_observations(run.id) if o.fingerprint.startswith("slow_")]
    assert len(found) == 2 and all(o.evidence.get("plan") for o in found)


def test_query_store_values_never_reach_the_report(
    service: AnalyzerService, on_azure: Connection
) -> None:
    run = service.run(on_azure.id, ["workload"])

    exported = service.export(run.id, "json").decode() + service.export(run.id, "md").decode()

    assert "alice@example.com" not in exported
    assert "customer_email = $1" in exported


def test_auxiliary_session_statements_are_audited_under_the_connection(
    service: AnalyzerService, on_azure: Connection
) -> None:
    service.run(on_azure.id, ["workload"])

    [read] = [e for e in service.audit(on_azure.id) if "query_store.qs_view" in e.sql]

    assert read.connection_id == on_azure.id
    assert read.decision == "executed" and read.purpose == "workload (azure_sys)"
    assert read.plan_cost is not None  # the EXPLAIN gate checked it: not a catalog read


def test_the_auxiliary_session_is_not_a_connection(
    service: AnalyzerService, on_azure: Connection, tmp_path: Path
) -> None:
    service.run(on_azure.id, ["workload"])

    with sqlite3.connect(tmp_path / "db-analyzer.sqlite") as db:
        names = [r[0] for r in db.execute("SELECT name FROM connections")]

    assert names == [on_azure.name]
    with pytest.raises(KeyError):
        service.connection("azure_sys")


def test_a_query_store_the_role_cannot_read_leaves_the_run_partial_with_the_reason(
    service: AnalyzerService, on_azure: Connection
) -> None:
    major = int(on_azure.name.removeprefix("azureqs"))
    with psycopg.connect(fixture_dsn(major, "postgres", "azure_sys"), autocommit=True) as conn:
        conn.execute("REVOKE SELECT ON query_store.qs_view FROM PUBLIC")

    run = service.run(on_azure.id, ["workload"])

    report = service.workload(run.id)
    assert run.status == "partial" and "workload" not in run.scope
    assert report is not None and report.source == "azure_query_store"
    assert "permission denied" in (report.refused or "")
    assert "permission denied" in service.export(run.id, "md").decode()
