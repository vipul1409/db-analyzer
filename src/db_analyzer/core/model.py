"""Store-agnostic domain types. Terms follow CONTEXT.md."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

HostType = Literal["self_managed", "azure_flexible"]
AuditDecision = Literal["executed", "rejected", "failed"]


@dataclass(frozen=True)
class SessionLimits:
    statement_timeout: str = "30s"
    lock_timeout: str = "1s"
    work_mem: str = "32MB"


@dataclass(frozen=True)
class Connection:
    """A configured target: exactly one database. Holds a reference to the DSN, never the DSN."""

    id: str
    name: str
    adapter_kind: str
    dsn_env: str
    limits: SessionLimits = field(default_factory=SessionLimits)


@dataclass(frozen=True)
class Privileges:
    pg_monitor: bool
    pg_read_all_stats: bool
    readable_tables: int
    unreadable_tables: list[str]


@dataclass(frozen=True)
class StatsFreshness:
    database_stats_reset: datetime | None
    statements_stats_reset: datetime | None
    never_analyzed_tables: int
    oldest_analyze: datetime | None


@dataclass(frozen=True)
class ProbeResult:
    """What the analyzer can see on a Connection."""

    server_version_num: int
    server_version: str
    in_recovery: bool
    host_type: HostType
    extensions: dict[str, str]
    privileges: Privileges
    settings: dict[str, str | None]
    stats: StatsFreshness
    taken_at: datetime


@dataclass(frozen=True)
class AuditEntry:
    connection_id: str
    purpose: str
    sql: str
    decision: AuditDecision
    reason: str | None
    duration_ms: float | None
    row_count: int | None
    at: datetime
    thread_id: str | None = None


class ConnectionRefused(Exception):
    """The target cannot be analysed safely or is unsupported."""


class QueryRejected(Exception):
    """A statement was refused before reaching the server."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
