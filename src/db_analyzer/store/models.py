"""SQLite persistence models. Credentials are never stored: connections hold a DSN env-var name."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ConnectionRow(Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    adapter_kind: Mapped[str] = mapped_column(String)
    dsn_env: Mapped[str] = mapped_column(String)
    limits_json: Mapped[str] = mapped_column(Text)
    gate_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProbeRow(Base):
    __tablename__ = "probes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result_json: Mapped[str] = mapped_column(Text)


class AuditRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"), index=True)
    thread_id: Mapped[str | None] = mapped_column(String, index=True)
    purpose: Mapped[str] = mapped_column(String)
    sql: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    row_count: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"), index=True)
    thread_id: Mapped[str | None] = mapped_column(String)
    scope_json: Mapped[str] = mapped_column(Text)
    skipped_json: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String)


class SnapshotRow(Base):
    """Measured data per Run (StorageStats now, WorkloadItems later) for trend comparison."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    data_json: Mapped[str] = mapped_column(Text)


class FindingRow(Base):
    __tablename__ = "findings"
    __table_args__ = (UniqueConstraint("connection_id", "fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"))
    fingerprint: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    first_seen_run: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    last_seen_run: Mapped[str] = mapped_column(ForeignKey("runs.id"))


class ObservationRow(Base):
    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("finding_id", "run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    severity: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    recommendation: Mapped[str | None] = mapped_column(Text)
    ddl: Mapped[str | None] = mapped_column(Text)

    finding: Mapped[FindingRow] = relationship(lazy="joined")
