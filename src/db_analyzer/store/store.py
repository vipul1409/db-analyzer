"""Local SQLite store. Schema is managed by Alembic and upgraded on open."""

import dataclasses
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import ColumnElement, create_engine, select
from sqlalchemy.orm import Session

from db_analyzer.core.model import (
    AnalyzerName,
    AuditEntry,
    CollectionKind,
    CollectionRef,
    Connection,
    Finding,
    GateLimits,
    LLMRequestLog,
    Observation,
    Observed,
    ProbeResult,
    Run,
    RunStatus,
    SessionLimits,
    StorageStats,
    Thread,
)
from db_analyzer.store.models import (
    AliasRow,
    AuditRow,
    ConnectionRow,
    FindingRow,
    LLMRequestRow,
    ObservationRow,
    ProbeRow,
    RunRow,
    SnapshotRow,
    ThreadRow,
)

_STORAGE_STATS = "storage_stats"  # snapshots.kind for StorageStats; WorkloadItems come later

Scope = dict[AnalyzerName, list[CollectionRef]]
Skipped = dict[AnalyzerName, list[tuple[CollectionRef, str]]]

_MIGRATIONS = Path(__file__).parent / "migrations"


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
        cfg = Config()
        cfg.set_main_option("script_location", str(_MIGRATIONS))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        # The agent runs tools in parallel threads that all write here: WAL lets readers and
        # one writer proceed together, and writers wait for the lock instead of failing.
        # WAL is a property of the database file, so setting it once is enough.
        self._engine = create_engine(url, connect_args={"timeout": 30})
        with self._engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")

    def upsert_connection(
        self,
        name: str,
        adapter_kind: str,
        dsn_env: str,
        limits: SessionLimits | None,
        gate: GateLimits | None,
        alias_identifiers: bool | None = None,
    ) -> Connection:
        """Create or update by name. `None` for `limits`, `gate` or `alias_identifiers` keeps
        the existing setting (the default if new)."""
        with Session(self._engine) as s, s.begin():
            row = s.scalars(select(ConnectionRow).where(ConnectionRow.name == name)).first()
            if row is None:
                row = ConnectionRow(id=uuid.uuid4().hex, name=name, created_at=datetime.now(UTC))
                s.add(row)
            row.adapter_kind = adapter_kind
            row.dsn_env = dsn_env
            if limits is not None or row.limits_json is None:
                row.limits_json = json.dumps(dataclasses.asdict(limits or SessionLimits()))
            if gate is not None or row.gate_json is None:
                row.gate_json = json.dumps(dataclasses.asdict(gate or GateLimits()))
            if alias_identifiers is not None or row.alias_identifiers is None:
                row.alias_identifiers = bool(alias_identifiers)
            return _connection(row)

    def find_connection(self, name: str) -> Connection:
        with Session(self._engine) as s:
            row = s.scalars(select(ConnectionRow).where(ConnectionRow.name == name)).first()
            if row is None:
                raise KeyError(f"no connection named {name!r}")
            return _connection(row)

    def get_connection(self, connection_id: str) -> Connection:
        with Session(self._engine) as s:
            row = s.get(ConnectionRow, connection_id)
            if row is None:
                raise KeyError(f"unknown connection {connection_id}")
            return _connection(row)

    def aliases(self, connection_id: str) -> dict[str, str]:
        q = select(AliasRow).where(AliasRow.connection_id == connection_id)
        with Session(self._engine) as s:
            return {r.name: r.alias for r in s.scalars(q)}

    def add_aliases(self, connection_id: str, aliases: dict[str, str]) -> None:
        with Session(self._engine) as s, s.begin():
            s.add_all(
                AliasRow(connection_id=connection_id, name=n, alias=a) for n, a in aliases.items()
            )

    def save_probe(self, connection_id: str, result: ProbeResult) -> None:
        with Session(self._engine) as s, s.begin():
            s.add(
                ProbeRow(
                    connection_id=connection_id,
                    taken_at=result.taken_at,
                    result_json=json.dumps(dataclasses.asdict(result), default=str),
                )
            )

    def start_run(self, connection_id: str, thread_id: str | None) -> Run:
        row = RunRow(
            id=uuid.uuid4().hex,
            connection_id=connection_id,
            thread_id=thread_id,
            scope_json="{}",
            skipped_json="{}",
            started_at=datetime.now(UTC),
            finished_at=None,
            status="running",
        )
        with Session(self._engine) as s, s.begin():
            s.add(row)
            return _run(row)

    def finish_run(self, run_id: str, status: RunStatus, scope: Scope, skipped: Skipped) -> Run:
        with Session(self._engine) as s, s.begin():
            row = s.get_one(RunRow, run_id)
            row.status = status
            row.scope_json = json.dumps(
                {a: [_ref_json(r) for r in refs] for a, refs in scope.items()}
            )
            row.skipped_json = json.dumps(
                {a: [[_ref_json(r), why] for r, why in items] for a, items in skipped.items()}
            )
            row.finished_at = datetime.now(UTC)
            return _run(row)

    def get_run(self, run_id: str) -> Run:
        with Session(self._engine) as s:
            return _run(s.get_one(RunRow, run_id))

    def runs(self, connection_id: str) -> list[Run]:
        q = select(RunRow).where(RunRow.connection_id == connection_id)
        with Session(self._engine) as s:
            return [_run(r) for r in s.scalars(q.order_by(RunRow.started_at))]

    def save_snapshots(self, run_id: str, measured: list[StorageStats]) -> None:
        with Session(self._engine) as s, s.begin():
            s.add_all(
                SnapshotRow(
                    run_id=run_id,
                    kind=_STORAGE_STATS,
                    subject=m.ref.qualified,
                    data_json=json.dumps(dataclasses.asdict(m)),
                )
                for m in measured
            )

    def snapshots(self, run_id: str) -> list[StorageStats]:
        q = select(SnapshotRow).where(
            SnapshotRow.run_id == run_id, SnapshotRow.kind == _STORAGE_STATS
        )
        with Session(self._engine) as s:
            return [
                _storage_stats(json.loads(r.data_json))
                for r in s.scalars(q.order_by(SnapshotRow.id))
            ]

    def record_observations(
        self, connection_id: str, run_id: str, observed: list[Observed]
    ) -> None:
        """Upsert each Finding by fingerprint and add this Run's Observation of it."""
        with Session(self._engine) as s, s.begin():
            for o in observed:
                finding = s.scalars(
                    select(FindingRow).where(
                        FindingRow.connection_id == connection_id,
                        FindingRow.fingerprint == o.fingerprint,
                    )
                ).first()
                if finding is None:
                    finding = FindingRow(
                        connection_id=connection_id,
                        fingerprint=o.fingerprint,
                        category=o.category,
                        subject=o.subject,
                        status="open",
                        first_seen_run=run_id,
                    )
                    s.add(finding)
                finding.last_seen_run = run_id
                s.flush()
                s.add(
                    ObservationRow(
                        finding_id=finding.id,
                        run_id=run_id,
                        severity=o.severity,
                        title=o.title,
                        evidence_json=json.dumps(o.evidence),
                        recommendation=o.recommendation,
                        ddl=o.ddl,
                    )
                )

    def findings(self, connection_id: str) -> list[Finding]:
        q = select(FindingRow).where(FindingRow.connection_id == connection_id)
        with Session(self._engine) as s:
            return [_finding(r) for r in s.scalars(q.order_by(FindingRow.id))]

    def observations(self, connection_id: str, fingerprint: str) -> list[Observation]:
        return self._observations(
            FindingRow.connection_id == connection_id, FindingRow.fingerprint == fingerprint
        )

    def run_observations(self, run_id: str) -> list[Observation]:
        return self._observations(ObservationRow.run_id == run_id)

    def _observations(self, *where: ColumnElement[bool]) -> list[Observation]:
        q = select(ObservationRow).join(ObservationRow.finding).where(*where)
        with Session(self._engine) as s:
            return [_observation(r) for r in s.scalars(q.order_by(ObservationRow.id))]

    def create_thread(self, connection_id: str) -> Thread:
        row = ThreadRow(
            id=uuid.uuid4().hex, connection_id=connection_id, created_at=datetime.now(UTC)
        )
        with Session(self._engine) as s, s.begin():
            s.get_one(ConnectionRow, connection_id)
            s.add(row)
            return _thread(row)

    def get_thread(self, thread_id: str) -> Thread:
        with Session(self._engine) as s:
            row = s.get(ThreadRow, thread_id)
            if row is None:
                raise KeyError(f"unknown thread {thread_id}")
            return _thread(row)

    def record_llm_request(self, thread_id: str, entry: LLMRequestLog) -> None:
        with Session(self._engine) as s, s.begin():
            s.add(LLMRequestRow(thread_id=thread_id, **dataclasses.asdict(entry)))

    def llm_requests(self, thread_id: str) -> list[LLMRequestLog]:
        q = select(LLMRequestRow).where(LLMRequestRow.thread_id == thread_id)
        with Session(self._engine) as s:
            return [
                LLMRequestLog(
                    model=r.model,
                    at=r.at.replace(tzinfo=UTC),
                    duration_ms=r.duration_ms,
                    request=r.request,
                    response=r.response,
                    input_tokens=r.input_tokens,
                    cached_tokens=r.cached_tokens,
                    output_tokens=r.output_tokens,
                    cost_usd=r.cost_usd,
                )
                for r in s.scalars(q.order_by(LLMRequestRow.id))
            ]

    def record_audit(self, entry: AuditEntry) -> None:
        with Session(self._engine) as s, s.begin():
            s.add(AuditRow(**dataclasses.asdict(entry)))

    def audit(self, connection_id: str, thread_id: str | None = None) -> list[AuditEntry]:
        q = select(AuditRow).where(AuditRow.connection_id == connection_id)
        if thread_id is not None:
            q = q.where(AuditRow.thread_id == thread_id)
        with Session(self._engine) as s:
            return [_audit_entry(r) for r in s.scalars(q.order_by(AuditRow.id))]


def _connection(row: ConnectionRow) -> Connection:
    return Connection(
        id=row.id,
        name=row.name,
        adapter_kind=row.adapter_kind,
        dsn_env=row.dsn_env,
        limits=SessionLimits(**json.loads(row.limits_json)),
        gate=GateLimits(**json.loads(row.gate_json)),
        alias_identifiers=row.alias_identifiers,
    )


def _thread(row: ThreadRow) -> Thread:
    return Thread(
        id=row.id, connection_id=row.connection_id, created_at=row.created_at.replace(tzinfo=UTC)
    )


def _ref_json(ref: CollectionRef) -> dict[str, str | None]:
    return {"namespace": ref.namespace, "name": ref.name, "kind": ref.kind.value}


def _ref(data: dict[str, str]) -> CollectionRef:
    return CollectionRef(data["namespace"], data["name"], CollectionKind(data["kind"]))


def _run(row: RunRow) -> Run:
    scope = json.loads(row.scope_json)
    skipped = json.loads(row.skipped_json)
    return Run(
        id=row.id,
        connection_id=row.connection_id,
        thread_id=row.thread_id,
        scope={a: [_ref(r) for r in refs] for a, refs in scope.items()},
        skipped={a: [(_ref(r), why) for r, why in items] for a, items in skipped.items()},
        started_at=row.started_at.replace(tzinfo=UTC),
        finished_at=row.finished_at.replace(tzinfo=UTC) if row.finished_at else None,
        status=row.status,  # type: ignore[arg-type]
    )


def _storage_stats(data: dict[str, Any]) -> StorageStats:
    return StorageStats(**{**data, "ref": _ref(data["ref"])})


def _finding(row: FindingRow) -> Finding:
    return Finding(
        connection_id=row.connection_id,
        fingerprint=row.fingerprint,
        category=row.category,  # type: ignore[arg-type]
        subject=row.subject,
        status=row.status,  # type: ignore[arg-type]
        first_seen_run=row.first_seen_run,
        last_seen_run=row.last_seen_run,
    )


def _observation(row: ObservationRow) -> Observation:
    return Observation(
        run_id=row.run_id,
        fingerprint=row.finding.fingerprint,
        severity=row.severity,  # type: ignore[arg-type]
        title=row.title,
        evidence=json.loads(row.evidence_json),
        recommendation=row.recommendation,
        ddl=row.ddl,
    )


def _audit_entry(row: AuditRow) -> AuditEntry:
    return AuditEntry(
        connection_id=row.connection_id,
        purpose=row.purpose,
        sql=row.sql,
        decision=row.decision,  # type: ignore[arg-type]
        reason=row.reason,
        duration_ms=row.duration_ms,
        row_count=row.row_count,
        at=row.at.replace(tzinfo=UTC),
        thread_id=row.thread_id,
        plan_cost=row.plan_cost,
        plan_rows=row.plan_rows,
    )
