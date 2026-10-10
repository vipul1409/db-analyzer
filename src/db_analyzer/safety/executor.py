"""SafeExecutor: the only path from analyzer code to the database.

query cap → guard → privacy check (LLM SQL) → read-only transaction (always rolled back) →
EXPLAIN gate (unless the guard exempts the statement) → execute → result filter → audit. The
guard profile follows from the method called: `execute` for vetted templates and workload text,
`execute_agent` for SQL the LLM wrote. Only LLM SQL passes the privacy filter here; templates
pass on their declared columns only (adapters/sql_common/templates.py). An auxiliary session
to a second database (`auxiliary`) runs through the same pipeline, cap and audit.
"""

import contextlib
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from db_analyzer.core.model import (
    AuditDecision,
    AuditEntry,
    EntityKey,
    GateLimits,
    PlanMetrics,
    QueryCapReached,
    QueryRejected,
)
from db_analyzer.safety import gate as explain_gate
from db_analyzer.safety import guard, privacy

Row = dict[str, Any]
AuditSink = Callable[[AuditEntry], None]
# Opens a hardened session to another database with the Connection's credentials and limits.
AuxiliaryOpener = Callable[[str], AbstractContextManager[psycopg.Connection[Any]]]

MAX_QUERIES_PER_TURN = 50


class QueryBudget:
    """Statements allowed in one agent turn. Every attempt counts, rejected ones included, so a
    loop of refused statements stops too. The caller creates one per turn."""

    def __init__(self, limit: int = MAX_QUERIES_PER_TURN):
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()  # the agent runs independent tools in parallel

    def spend(self) -> None:
        with self._lock:
            if self.used >= self.limit:
                raise QueryCapReached(self.limit)
            self.used += 1

    def reset(self) -> None:
        with self._lock:
            self.used = 0


class SafeExecutor:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        connection_id: str,
        audit: AuditSink,
        gate: GateLimits | None = None,
        budget: QueryBudget | None = None,
        thread_id: str | None = None,
        entity_keys: frozenset[EntityKey] = frozenset(),
        open_auxiliary: AuxiliaryOpener | None = None,
        database: str | None = None,
    ):
        """`database` names the database of an auxiliary session; its audit entries say so."""
        self._conn = conn
        self._connection_id = connection_id
        self._audit = audit
        self._gate = gate or GateLimits()
        self._budget = budget
        self._thread_id = thread_id
        self._entity_keys = entity_keys
        self._open_auxiliary = open_auxiliary
        self._database = database

    @property
    def server_version_num(self) -> int:
        """From the connection handshake: no statement is run."""
        return self._conn.info.server_version

    @contextlib.contextmanager
    def auxiliary(self, database: str) -> Iterator["SafeExecutor"]:
        """An auxiliary session: a second database the Connection needs (e.g. `azure_sys` for
        Query Store), opened with its credentials and limits. Its statements pass the same
        guard, gate and query cap, and are audited under the same Connection and Thread."""
        if self._open_auxiliary is None:
            raise RuntimeError(f"this executor cannot open auxiliary sessions ({database})")
        with self._open_auxiliary(database) as conn:
            yield SafeExecutor(
                conn,
                self._connection_id,
                self._audit,
                self._gate,
                self._budget,
                self._thread_id,
                self._entity_keys,
                database=database,
            )

    def execute(self, sql: str, purpose: str) -> list[Row]:
        """Run vetted SQL (templates, verbatim workload text) under the internal profile."""
        return self._run([sql], purpose, "internal")

    def execute_agent(self, sql: str, purpose: str) -> list[Row]:
        """Run SQL the LLM wrote, under the agent profile."""
        return self._run([sql], purpose, "agent")

    def execute_sequence(
        self, statements: Sequence[str], purpose: str, cleanup: str | None = None
    ) -> list[Row]:
        """Run vetted statements in order in one read-only transaction, under the internal
        profile, for statements that depend on earlier ones (SET LOCAL, PREPARE). Every
        statement passes the guard before any runs. Returns the last statement's rows.
        `cleanup` runs afterwards on its own, whether the sequence succeeded or not: a prepared
        statement outlives the rollback (ADR 0002). After a failure the cleanup may fail too
        (nothing was prepared): that is audited, and the first error is the one raised."""
        try:
            rows = self._run(statements, purpose, "internal")
        except BaseException:
            if cleanup is not None:
                with contextlib.suppress(QueryRejected, psycopg.Error):
                    self._run([cleanup], purpose, "internal")
            raise
        if cleanup is not None:
            self._run([cleanup], purpose, "internal")
        return rows

    def _run(self, statements: Sequence[str], purpose: str, profile: guard.Profile) -> list[Row]:
        checked = [self._check(sql, purpose, profile) for sql in statements]
        rows: list[Row] = []
        with (
            self._conn.transaction(force_rollback=True),
            self._conn.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute("SET TRANSACTION READ ONLY")
            for sql, (kind, output) in zip(statements, checked, strict=True):
                rows = self._statement(cur, sql, purpose, kind, output)
        return rows

    def _check(
        self, sql: str, purpose: str, profile: guard.Profile
    ) -> tuple[guard.Checked, privacy.OutputFilter]:
        try:
            if self._budget is not None:
                self._budget.spend()
            checked = guard.check(sql, profile)
            output = (
                privacy.check_output(sql, self._entity_keys)
                if profile == "agent"
                else privacy.OutputFilter()
            )
        except QueryRejected as e:
            self._record(sql, purpose, "rejected", e.reason)
            raise
        return checked, output

    def _statement(
        self,
        cur: psycopg.Cursor[Row],
        sql: str,
        purpose: str,
        checked: guard.Checked,
        output: privacy.OutputFilter,
    ) -> list[Row]:
        """One statement inside the read-only transaction: EXPLAIN gate, execute, filter."""
        plan: PlanMetrics | None = None
        started = time.perf_counter()
        try:
            if checked.gated:
                # The guard has accepted exactly one SELECT, so wrapping it is safe.
                cur.execute(b"EXPLAIN (FORMAT JSON, COSTS ON) " + sql.encode())
                explained = cur.fetchone()
                if explained is None:
                    raise QueryRejected("EXPLAIN returned no plan")
                plan = explain_gate.check(explained["QUERY PLAN"], self._gate)
            cur.execute(sql.encode())
            rows = output.apply(cur.fetchall() if cur.description else [])
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
                purpose=purpose if self._database is None else f"{purpose} ({self._database})",
                sql=sql,
                decision=decision,
                reason=reason,
                duration_ms=duration_ms,
                row_count=row_count,
                at=datetime.now(UTC),
                thread_id=self._thread_id,
                plan_cost=plan.total_cost if plan else None,
                plan_rows=plan.result_rows if plan else None,
            )
        )


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
