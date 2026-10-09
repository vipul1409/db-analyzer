"""Local SQLite store. Schema is managed by Alembic and upgraded on open."""

import dataclasses
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from db_analyzer.core.model import AuditEntry, Connection, ProbeResult, SessionLimits
from db_analyzer.store.models import AuditRow, ConnectionRow, ProbeRow

_MIGRATIONS = Path(__file__).parent / "migrations"


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
        cfg = Config()
        cfg.set_main_option("script_location", str(_MIGRATIONS))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        self._engine = create_engine(url)

    def upsert_connection(
        self, name: str, adapter_kind: str, dsn_env: str, limits: SessionLimits | None
    ) -> Connection:
        """Create or update by name. `limits=None` keeps existing limits (defaults if new)."""
        with Session(self._engine) as s, s.begin():
            row = s.scalars(select(ConnectionRow).where(ConnectionRow.name == name)).first()
            if row is None:
                row = ConnectionRow(id=uuid.uuid4().hex, name=name, created_at=datetime.now(UTC))
                s.add(row)
            row.adapter_kind = adapter_kind
            row.dsn_env = dsn_env
            if limits is not None or row.limits_json is None:
                row.limits_json = json.dumps(dataclasses.asdict(limits or SessionLimits()))
            return _connection(row)

    def get_connection(self, connection_id: str) -> Connection:
        with Session(self._engine) as s:
            row = s.get(ConnectionRow, connection_id)
            if row is None:
                raise KeyError(f"unknown connection {connection_id}")
            return _connection(row)

    def save_probe(self, connection_id: str, result: ProbeResult) -> None:
        with Session(self._engine) as s, s.begin():
            s.add(
                ProbeRow(
                    connection_id=connection_id,
                    taken_at=result.taken_at,
                    result_json=json.dumps(dataclasses.asdict(result), default=str),
                )
            )

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
    )
