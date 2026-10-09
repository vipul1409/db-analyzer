"""SafeExecutor: the only path from analyzer code to the database.

guard → read-only transaction (always rolled back) → EXPLAIN gate → execute → audit. Gate
exemptions by statement type and the result filter are added in later tickets.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from db_analyzer.core.model import AuditDecision, AuditEntry, GateLimits, QueryRejected
from db_analyzer.safety import gate as explain_gate
from db_analyzer.safety import guard

Row = dict[str, Any]
AuditSink = Callable[[AuditEntry], None]


class SafeExecutor:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        connection_id: str,
        audit: AuditSink,
        gate: GateLimits | None = None,
    ):
        self._conn = conn
        self._connection_id = connection_id
        self._audit = audit
        self._gate = gate or GateLimits()

    def execute(self, sql: str, purpose: str) -> list[Row]:
        try:
            guard.check(sql)
        except QueryRejected as e:
            self._record(sql, purpose, "rejected", e.reason, None, None)
            raise
        started = time.perf_counter()
        try:
            with (
                self._conn.transaction(force_rollback=True),
                self._conn.cursor(row_factory=dict_row) as cur,
            ):
                cur.execute("SET TRANSACTION READ ONLY")
                # The guard has accepted exactly one SELECT, so wrapping it is safe.
                cur.execute(b"EXPLAIN (FORMAT JSON, COSTS ON) " + sql.encode())
                plan = cur.fetchone()
                if plan is None:
                    raise QueryRejected("EXPLAIN returned no plan")
                explain_gate.check(plan["QUERY PLAN"], self._gate)
                cur.execute(sql.encode())
                rows = cur.fetchall() if cur.description else []
        except QueryRejected as e:
            self._record(sql, purpose, "rejected", e.reason, None, None)
            raise
        except psycopg.Error as e:
            self._record(sql, purpose, "failed", str(e).strip(), _ms(started), None)
            raise
        self._record(sql, purpose, "executed", None, _ms(started), len(rows))
        return rows

    def _record(
        self,
        sql: str,
        purpose: str,
        decision: AuditDecision,
        reason: str | None,
        duration_ms: float | None,
        row_count: int | None,
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
            )
        )


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
