"""SafeExecutor: the only path from analyzer code to the database.

guard → read-only transaction (always rolled back) → audit. The EXPLAIN gate and result
filter are added in later tickets.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from db_analyzer.core.model import AuditDecision, AuditEntry, QueryRejected
from db_analyzer.safety import guard

Row = dict[str, Any]
AuditSink = Callable[[AuditEntry], None]


class SafeExecutor:
    def __init__(self, conn: psycopg.Connection[Any], connection_id: str, audit: AuditSink):
        self._conn = conn
        self._connection_id = connection_id
        self._audit = audit

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
                cur.execute(sql.encode())
                rows = cur.fetchall() if cur.description else []
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
