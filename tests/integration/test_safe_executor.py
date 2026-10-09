from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from db_analyzer.adapters.postgres.session import open_session
from db_analyzer.core.model import (
    AuditEntry,
    GateLimits,
    GateRejected,
    QueryRejected,
    SessionLimits,
)
from db_analyzer.safety.executor import SafeExecutor

from .conftest import SUPPORTED, dsn

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
