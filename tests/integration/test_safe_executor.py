from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from db_analyzer.adapters.postgres.queries import LIBRARY
from db_analyzer.adapters.postgres.session import open_session
from db_analyzer.core.model import (
    AuditEntry,
    EntityKey,
    GateLimits,
    GateRejected,
    PrivacyRejected,
    QueryCapReached,
    QueryRejected,
    SessionLimits,
)
from db_analyzer.safety import privacy
from db_analyzer.safety.executor import QueryBudget, SafeExecutor

from .conftest import SUPPORTED, dsn, seeded_dsn

pytestmark = pytest.mark.integration


@pytest.fixture(params=SUPPORTED)
def session(request: pytest.FixtureRequest) -> Iterator[psycopg.Connection[Any]]:
    with open_session(dsn(request.param), SessionLimits(statement_timeout="7s")) as conn:
        yield conn


def test_session_is_hardened(session: psycopg.Connection[Any]) -> None:
    executor = SafeExecutor(session, "c1", audit=lambda _: None)

    [row] = executor.execute(
        """SELECT current_setting('default_transaction_read_only') AS read_only,
                  current_setting('transaction_read_only') AS txn_read_only,
                  current_setting('statement_timeout') AS statement_timeout,
                  current_setting('lock_timeout') AS lock_timeout,
                  current_setting('application_name') AS application_name,
                  current_setting('work_mem') AS work_mem""",
        purpose="test",
    )

    assert row == {
        "read_only": "on",
        "txn_read_only": "on",
        "statement_timeout": "7s",
        "lock_timeout": "1s",
        "application_name": "db-analyzer",
        "work_mem": "32MB",
    }


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM private.notes",
        "CREATE TABLE t (x int)",
        "SELECT 1; SELECT 2",
        "SELECT * INTO t2 FROM pg_class",
        "SELECT * FROM pg_class FOR UPDATE",
        "WITH d AS (DELETE FROM private.notes RETURNING *) SELECT * FROM d",
        "SELECT 1 UNION SELECT * FROM (SELECT 1 FROM pg_class FOR SHARE) s",
        "not sql at all",
    ],
)
def test_unsafe_statement_is_rejected_and_audited(
    session: psycopg.Connection[Any], sql: str
) -> None:
    audit: list[AuditEntry] = []
    executor = SafeExecutor(session, "c1", audit=audit.append)

    with pytest.raises(QueryRejected):
        executor.execute(sql, purpose="test")

    assert [(e.sql, e.decision) for e in audit] == [(sql, "rejected")]
    assert audit[0].reason


def test_executed_statement_is_audited_with_rows(session: psycopg.Connection[Any]) -> None:
    audit: list[AuditEntry] = []
    executor = SafeExecutor(session, "c1", audit=audit.append)

    rows = executor.execute("SELECT generate_series(1, 3) AS n", purpose="test")

    assert [r["n"] for r in rows] == [1, 2, 3]
    assert [(e.decision, e.row_count) for e in audit] == [("executed", 3)]


def test_statement_over_gate_limits_is_rejected_and_audited(
    session: psycopg.Connection[Any],
) -> None:
    audit: list[AuditEntry] = []
    executor = SafeExecutor(session, "c1", audit=audit.append, gate=GateLimits(max_result_rows=10))
    sql = "SELECT generate_series(1, 1000) AS n"

    with pytest.raises(GateRejected) as e:
        executor.execute(sql, purpose="test")

    assert e.value.metric == "result_rows"
    assert [(a.sql, a.decision, a.reason) for a in audit] == [(sql, "rejected", e.value.reason)]


def test_statement_within_gate_limits_runs(session: psycopg.Connection[Any]) -> None:
    executor = SafeExecutor(
        session, "c1", audit=lambda _: None, gate=GateLimits(max_result_rows=10)
    )

    assert len(executor.execute("SELECT generate_series(1, 5) AS n", purpose="test")) == 5


def test_agent_sql_runs_under_the_agent_profile(session: psycopg.Connection[Any]) -> None:
    audit: list[AuditEntry] = []
    executor = SafeExecutor(session, "c1", audit=audit.append)
    sql = "PREPARE p AS SELECT 1"

    with pytest.raises(QueryRejected, match="agent profile"):
        executor.execute_agent(sql, purpose="agent")
    executor.execute(sql, purpose="test")  # internal profile: vetted code path

    assert [(a.sql, a.decision) for a in audit] == [(sql, "rejected"), (sql, "executed")]
    executor.execute("DEALLOCATE p", purpose="test")


def test_catalog_only_template_is_exempt_from_the_gate(session: psycopg.Connection[Any]) -> None:
    audit: list[AuditEntry] = []
    tiny = GateLimits(max_total_cost=1, max_result_rows=1, max_scan_rows=1)
    executor = SafeExecutor(session, "c1", audit=audit.append, gate=tiny)
    storage_stats = LIBRARY.get("storage_stats", session.info.server_version).sql

    assert executor.execute(storage_stats, purpose="test")
    assert (audit[0].decision, audit[0].plan_cost) == ("executed", None)


def test_gated_statement_records_its_plan_in_the_audit(session: psycopg.Connection[Any]) -> None:
    audit: list[AuditEntry] = []
    executor = SafeExecutor(session, "c1", audit=audit.append)

    executor.execute("SELECT generate_series(1, 5) AS n", purpose="test")

    assert audit[0].plan_cost is not None and audit[0].plan_cost > 0
    assert audit[0].plan_rows == 5


def test_query_cap_stops_with_a_structured_reason(session: psycopg.Connection[Any]) -> None:
    audit: list[AuditEntry] = []
    executor = SafeExecutor(session, "c1", audit=audit.append, budget=QueryBudget(2))
    executor.execute_agent("SELECT 1", purpose="agent")
    executor.execute_agent("SELECT 2", purpose="agent")

    with pytest.raises(QueryCapReached) as e:
        executor.execute_agent("SELECT 3", purpose="agent")

    assert e.value.limit == 2
    assert e.value.reason == "query cap reached: 2 statements this turn"
    assert [a.decision for a in audit] == ["executed", "executed", "rejected"]


@pytest.fixture(params=SUPPORTED)
def shop(request: pytest.FixtureRequest) -> Iterator[psycopg.Connection[Any]]:
    with open_session(seeded_dsn(request.param), SessionLimits()) as conn:
        yield conn


def test_row_data_the_llm_asks_for_is_rejected_before_it_runs_and_audited(
    shop: psycopg.Connection[Any],
) -> None:
    audit: list[AuditEntry] = []
    executor = SafeExecutor(shop, "c1", audit=audit.append)
    sql = "SELECT email FROM accounts ORDER BY id LIMIT 3"

    with pytest.raises(PrivacyRejected, match="email"):
        executor.execute_agent(sql, purpose="agent")

    assert [(a.sql, a.decision, a.plan_cost) for a in audit] == [(sql, "rejected", None)]
    assert audit[0].reason and "email" in audit[0].reason


def test_aggregate_sql_from_the_llm_runs(shop: psycopg.Connection[Any]) -> None:
    executor = SafeExecutor(shop, "c1", audit=lambda _: None)

    rows = executor.execute_agent(
        "SELECT count(*) AS pending FROM bookings WHERE status = 'pending'", purpose="agent"
    )

    assert rows == [{"pending": 2_000}]


def test_sampled_values_reach_the_llm_only_for_entity_keys(shop: psycopg.Connection[Any]) -> None:
    key = EntityKey("public", "events", "tenant_id")
    executor = SafeExecutor(shop, "c1", audit=lambda _: None, entity_keys=frozenset({key}))

    rows = executor.execute_agent(
        """SELECT schemaname, tablename, attname, most_common_vals FROM pg_stats
           WHERE schemaname = 'public' AND attname = 'tenant_id'
             AND tablename IN ('events', 'accounts')""",
        purpose="agent",
    )

    shown = {r["tablename"]: r["most_common_vals"] for r in rows}
    assert "7" in str(shown["events"])
    assert shown["accounts"] == privacy.WITHHELD


def test_vetted_sql_is_not_subject_to_the_llm_output_check(
    shop: psycopg.Connection[Any],
) -> None:
    executor = SafeExecutor(shop, "c1", audit=lambda _: None)

    assert executor.execute("SELECT email FROM accounts ORDER BY id LIMIT 1", purpose="test")
