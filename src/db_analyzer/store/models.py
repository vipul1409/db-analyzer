"""SQLite persistence models. Credentials are never stored: connections hold a DSN env-var name."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ConnectionRow(Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    adapter_kind: Mapped[str] = mapped_column(String)
    dsn_env: Mapped[str] = mapped_column(String)
    limits_json: Mapped[str] = mapped_column(Text)
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
