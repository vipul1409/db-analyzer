"""Store-agnostic domain types. Terms follow CONTEXT.md."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

HostType = Literal["self_managed", "azure_flexible"]
AuditDecision = Literal["executed", "rejected", "failed"]


@dataclass(frozen=True)
class SessionLimits:
    statement_timeout: str = "30s"
    lock_timeout: str = "1s"
    work_mem: str = "32MB"


@dataclass(frozen=True)
class GateLimits:
    """EXPLAIN gate limits; a statement whose plan exceeds any of them is not run."""

    max_total_cost: float = 2_000_000  # planner cost units
    max_result_rows: int = 10_000  # rows returned to the caller
    max_scan_rows: int = 50_000_000  # rows any single scan may touch


@dataclass(frozen=True)
class Connection:
    """A configured target: exactly one database. Holds a reference to the DSN, never the DSN."""

    id: str
    name: str
    adapter_kind: str
    dsn_env: str
    limits: SessionLimits = field(default_factory=SessionLimits)
    gate: GateLimits = field(default_factory=GateLimits)


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


class Capability(StrEnum):
    """What an adapter can do on a Connection. The agent gets a tool only for capabilities the
    Connection has."""

    PROBE = "probe"
    STORAGE_STATS = "storage_stats"


class CollectionKind(StrEnum):
    TABLE = "table"
    PARTITIONED_TABLE = "partitioned_table"
    MATVIEW = "matview"


@dataclass(frozen=True)
class CollectionRef:
    namespace: str | None
    name: str
    kind: CollectionKind

    @property
    def qualified(self) -> str:
        """Schema-qualified name, quoted only where Postgres would need it."""
        return ".".join(_quote(p) for p in (self.namespace, self.name) if p is not None)


def _quote(identifier: str) -> str:
    if re.fullmatch(r"[a-z_][a-z0-9_$]*", identifier):
        return identifier
    return '"' + identifier.replace('"', '""') + '"'


RowCountMethod = Literal["exact", "estimate", "sample"]


@dataclass(frozen=True)
class StorageStats:
    """Size of one collection. `row_count` is None when the store has no estimate yet."""

    ref: CollectionRef
    row_count: int | None
    row_count_method: RowCountMethod
    data_bytes: int
    index_bytes: int
    toast_bytes: int | None
    total_bytes: int


AnalyzerName = Literal["inventory", "workload", "index_advice", "hotspot"]
RunStatus = Literal["running", "complete", "partial", "failed"]


@dataclass(frozen=True)
class Run:
    """One execution of analyzers against a Connection. `scope` lists, per analyzer, the
    collections actually measured; `skipped` those planned but not measured, with the reason."""

    id: str
    connection_id: str
    thread_id: str | None
    scope: dict[AnalyzerName, list[CollectionRef]]
    skipped: dict[AnalyzerName, list[tuple[CollectionRef, str]]]
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus


@dataclass(frozen=True)
class Thread:
    """A conversation with the agent, bound to one Connection for its whole life."""

    id: str
    connection_id: str
    created_at: datetime


FindingCategory = Literal[
    "size",
    "slow_query",
    "missing_index",
    "unused_index",
    "duplicate_index",
    "invalid_index",
    "bloat",
    "stale_stats",
    "hotspot",
    "config",
]
FindingStatus = Literal["open", "acknowledged", "fixed", "obsolete"]
Severity = Literal["info", "low", "medium", "high"]


@dataclass(frozen=True)
class Observed:
    """What an analyzer saw in this Run, before it is recorded as a Finding and Observation."""

    category: FindingCategory
    subject: str
    severity: Severity
    title: str
    evidence: dict[str, Any]
    recommendation: str | None = None
    ddl: str | None = None

    @property
    def fingerprint(self) -> str:
        return f"{self.category}:{self.subject}"


@dataclass(frozen=True)
class Finding:
    """A problem or fact about one subject, with an identity stable across Runs."""

    connection_id: str
    fingerprint: str
    category: FindingCategory
    subject: str
    status: FindingStatus
    first_seen_run: str
    last_seen_run: str


@dataclass(frozen=True)
class Observation:
    """What one Run saw for one Finding."""

    run_id: str
    fingerprint: str
    severity: Severity
    title: str
    evidence: dict[str, Any]
    recommendation: str | None
    ddl: str | None


@dataclass(frozen=True)
class LLMRequestLog:
    """One model request, as the LLMGateway logs it."""

    model: str
    at: datetime
    duration_ms: float
    request: str  # redacted, truncated
    response: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float | None  # None for a model without a known price


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
    plan_cost: float | None = None  # EXPLAIN total cost, for statements the gate checked
    plan_rows: int | None = None  # EXPLAIN estimated result rows


class ConnectionRefused(Exception):
    """The target cannot be analysed safely or is unsupported."""


class QueryRejected(Exception):
    """A statement was refused before reaching the server."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


GateMetric = Literal["total_cost", "result_rows", "scan_rows"]


class QueryCapReached(QueryRejected):
    """The per-turn query cap stopped a statement: a runaway-loop guard."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"query cap reached: {limit} statements this turn")
        self.limit = limit


@dataclass(frozen=True)
class PlanMetrics:
    """What the EXPLAIN gate read from a plan."""

    total_cost: float
    result_rows: int
    scan_rows: int


class GateRejected(QueryRejected):
    """The EXPLAIN gate refused a statement: its plan exceeds one of the GateLimits."""

    def __init__(self, metric: GateMetric, value: float, limit: float) -> None:
        super().__init__(f"{metric.replace('_', ' ')} {value:.2g} > {limit:.2g}")
        self.metric = metric
        self.value = value
        self.limit = limit
