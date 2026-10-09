"""SafeExecutor: the only path from analyzer code to the database.

query cap → guard → read-only transaction (always rolled back) → EXPLAIN gate (unless the guard
exempts the statement) → execute → audit. The guard profile follows from the method called:
`execute` for vetted templates and workload text, `execute_agent` for SQL the LLM wrote. The
result filter is added in a later ticket.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from db_analyzer.core.model import (
    AuditDecision,
    AuditEntry,
    GateLimits,
    PlanMetrics,
    QueryCapReached,
    QueryRejected,
)
from db_analyzer.safety import gate as explain_gate
from db_analyzer.safety import guard

Row = dict[str, Any]
AuditSink = Callable[[AuditEntry], None]

MAX_QUERIES_PER_TURN = 50


class QueryBudget:
    """Statements allowed in one agent turn. Every attempt counts, rejected ones included, so a
    loop of refused statements stops too. The caller creates one per turn."""

    def __init__(self, limit: int = MAX_QUERIES_PER_TURN):
        self.limit = limit
        self.used = 0

    def spend(self) -> None:
        if self.used >= self.limit:
            raise QueryCapReached(self.limit)
        self.used += 1


class SafeExecutor:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        connection_id: str,
        audit: AuditSink,
        gate: GateLimits | None = None,
        budget: QueryBudget | None = None,
    ):
        self._conn = conn
        self._connection_id = connection_id
        self._audit = audit
        self._gate = gate or GateLimits()
        self._budget = budget

    def execute(self, sql: str, purpose: str) -> list[Row]:
        """Run vetted SQL (templates, verbatim workload text) under the internal profile."""
        return self._run(sql, purpose, "internal")

    def execute_agent(self, sql: str, purpose: str) -> list[Row]:
        """Run SQL the LLM wrote, under the agent profile."""
        return self._run(sql, purpose, "agent")

    def _run(self, sql: str, purpose: str, profile: guard.Profile) -> list[Row]:
        try:
            if self._budget is not None:
                self._budget.spend()
            checked = guard.check(sql, profile)
        except QueryRejected as e:
            self._record(sql, purpose, "rejected", e.reason)
            raise
        plan: PlanMetrics | None = None
        started = time.perf_counter()
        try:
            with (
                self._conn.transaction(force_rollback=True),
                self._conn.cursor(row_factory=dict_row) as cur,
            ):
                cur.execute("SET TRANSACTION READ ONLY")
                if checked.gated:
                    # The guard has accepted exactly one SELECT, so wrapping it is safe.
                    cur.execute(b"EXPLAIN (FORMAT JSON, COSTS ON) " + sql.encode())
                    explained = cur.fetchone()
                    if explained is None:
                        raise QueryRejected("EXPLAIN returned no plan")
                    plan = explain_gate.check(explained["QUERY PLAN"], self._gate)
                cur.execute(sql.encode())
                rows = cur.fetchall() if cur.description else []
        except QueryRejected as e:
            self._record(sql, purpose, "rejected", e.reason)
            raise
        except psycopg.Error as e:
            self._record(sql, purpose, "failed", str(e).strip(), _ms(started), plan=plan)
            raise
        self._record(sql, purpose, "executed", None, _ms(started), row_count=len(rows), plan=plan)
        return rows

    def _record(
        self,
        sql: str,
        purpose: str,
        decision: AuditDecision,
        reason: str | None,
        duration_ms: float | None = None,
        row_count: int | None = None,
        plan: PlanMetrics | None = None,
    ) -> None:
        self._audit(
            AuditEntry(
                connection_id=self._connection_id,
                purpose=purpose,
                sql=sql,
                decision=decision,
                reason=reason,
                duration_ms=duration_ms,
                row_count=row_count,
                at=datetime.now(UTC),
                plan_cost=plan.total_cost if plan else None,
                plan_rows=plan.result_rows if plan else None,
            )
        )


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
