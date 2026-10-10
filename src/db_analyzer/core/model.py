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
    alias_identifiers: bool = False  # the LLM sees schema, table and column names as aliases


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
    extension_schemas: dict[str, str] = field(default_factory=dict)  # extension -> its schema


class Capability(StrEnum):
    """What an adapter can do on a Connection. The agent gets a tool only for capabilities the
    Connection has."""

    PROBE = "probe"
    STORAGE_STATS = "storage_stats"
    EXACT_COUNTS = "exact_counts"  # count(*) per table, where the EXPLAIN gate allows
    READONLY_SQL = "readonly_sql"  # ad-hoc SQL under the agent guard profile and privacy filter


@dataclass(frozen=True)
class EntityKey:
    """A column that identifies an entity type in one table, e.g. public.bookings.tenant_id."""

    schema: str
    table: str
    column: str


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
        return qualify(self.namespace, self.name)


def qualify(namespace: str | None, name: str) -> str:
    """Schema-qualified name of any relation, quoted only where Postgres would need it."""
    return ".".join(_quote(p) for p in (namespace, name) if p is not None)


def _quote(identifier: str) -> str:
    if re.fullmatch(r"[a-z_][a-z0-9_$]*", identifier):
        return identifier
    return '"' + identifier.replace('"', '""') + '"'


RowCountMethod = Literal["exact", "estimate", "sample"]


@dataclass(frozen=True)
class Maintenance:
    """Activity counters and vacuum/analyze recency of one collection (a partitioned table sums
    its leaf partitions and takes the oldest timestamp; None if any leaf never had it)."""

    live_rows: int  # the store's running count of live rows, kept between analyzes
    dead_rows: int
    modified_since_analyze: int
    last_vacuum: datetime | None  # manual or automatic, whichever is later
    last_analyze: datetime | None
    autovacuum_disabled: bool


@dataclass(frozen=True)
class DeadTupleScan:
    """A dead-tuple measurement that reads the table (pgstattuple_approx), not the counters."""

    live_rows: int
    dead_rows: int
    dead_percent: float  # of the table's bytes
    free_percent: float


@dataclass(frozen=True)
class StorageStats:
    """Size of one collection. `row_count` is None when the store has no estimate yet.
    `skipped` names measurements planned for this collection but not taken, with the reason
    (e.g. an exact count the EXPLAIN gate refused); the collection itself was measured."""

    ref: CollectionRef
    row_count: int | None
    row_count_method: RowCountMethod
    data_bytes: int
    index_bytes: int
    toast_bytes: int | None
    total_bytes: int
    partitions: int | None = None  # leaf partitions, for a partitioned table
    maintenance: Maintenance | None = None
    dead_tuple_scan: DeadTupleScan | None = None
    skipped: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexStats:
    """One index on a collection, from the catalog. `keys` identify each key column precisely
    (column or expression, operator class, collation, ordering) for comparing indexes and never
    leave the analyzer; `columns` names the key columns, None for an expression. A partitioned
    index sums its partitions' sizes and scans."""

    name: str  # schema-qualified
    table: CollectionRef
    method: str  # access method: btree, hash, gin, ...
    keys: list[str]
    columns: list[str | None]
    include: list[str]
    predicate: str | None  # a partial index's WHERE clause
    index_bytes: int
    scans: int  # since statistics were last reset
    unique: bool
    primary: bool
    constraint: bool  # backs a constraint (primary key, unique, exclusion)
    valid: bool
    partitioned: bool = False  # an index on a partitioned table
    nulls_not_distinct: bool = False  # a unique index that treats NULLs as equal


WorkloadSource = Literal["pg_stat_statements"]


@dataclass(frozen=True)
class WorkloadStatement:
    """One row of a workload source: a normalized statement and what it cost, summed over every
    user and server-side id it ran under."""

    text: str  # normalized: placeholders ($1) in place of values
    calls: int
    total_ms: float
    rows: int
    shared_blks_read: int
    temp_blks_written: int


@dataclass(frozen=True)
class WorkloadReading:
    """What a workload source holds now: its statements, and when its statistics were last
    reset (the start of the window they cover; None when unknown)."""

    statements: list[WorkloadStatement]
    stats_reset: datetime | None


@dataclass(frozen=True)
class WorkloadItem:
    """One distinct statement of the workload, ranked. `fingerprint` is a hash of the
    normalized text, never the server's query id, so it holds across versions."""

    fingerprint: str
    text: str
    calls: int
    total_ms: float
    mean_ms: float
    rows: int
    shared_blks_read: int
    temp_blks_written: int
    share_of_time: float  # of all the statements the source reported
    ranked_by: list[str]  # which of the four rankings it is in the top N of


@dataclass(frozen=True)
class WorkloadReport:
    """What the workload part of a Run found. `items` is empty and `refused` says why when the
    statistics cannot be ranked; `source` is None when the Connection has none."""

    source: WorkloadSource | None
    stats_reset: datetime | None
    window_seconds: float | None  # how long the source has been collecting
    items: list[WorkloadItem]
    statements: int  # distinct statements the source reported, after exclusions
    total_ms: float  # of those
    excluded: dict[str, int]  # reason -> rows left out of the ranking
    warnings: list[str]
    refused: str | None = None
    enable_steps: list[str] = field(default_factory=list)  # when there is no source


@dataclass(frozen=True)
class PlanNode:
    """One node of a normalized plan: the planner's estimates for it, never measured values (a
    generic plan executes nothing). `relation` and `index` are schema-qualified. `condition` is
    the part an index or join resolves (Index Cond, Recheck Cond, Hash/Merge Cond), `filter` the
    part checked row by row afterwards (Filter, Join Filter). `filter_columns` names the columns
    of `relation` the filter reads."""

    node_type: str
    rows: int  # estimated rows per execution of the node
    width: int  # estimated bytes per row
    total_cost: float
    relation: str | None = None
    index: str | None = None
    join_type: str | None = None
    strategy: str | None = None  # an aggregate's or set operation's: Plain, Sorted, Hashed, Mixed
    condition: str | None = None
    filter: str | None = None
    filter_columns: list[str] = field(default_factory=list)
    sort_key: list[str] = field(default_factory=list)
    children: list["PlanNode"] = field(default_factory=list)

    def walk(self) -> "list[PlanNode]":
        """This node and every node below it, depth first."""
        return [self, *(n for c in self.children for n in c.walk())]

    def line(self) -> str:
        """One line of EXPLAIN-like text for this node alone."""
        on = f" on {self.relation}" if self.relation else ""
        using = f" using {self.index}" if self.index else ""
        head = f"{self.join_type} " if self.join_type and self.join_type != "Inner" else ""
        text = f"{head}{self.node_type}{using}{on} (rows={self.rows:,} width={self.width})"
        if self.condition:
            text += f" cond: {self.condition}"
        if self.filter:
            text += f" filter: {self.filter}"
        if self.sort_key:
            text += f" key: {', '.join(self.sort_key)}"
        return text

    def text(self, depth: int = 0) -> list[str]:
        """The tree as indented lines, one per node."""
        return ["  " * depth + self.line(), *(t for c in self.children for t in c.text(depth + 1))]


@dataclass(frozen=True)
class RelationEstimate:
    """What the planner knows about one collection, or one partition of it (plans scan
    partitions), for judging the estimates in a plan."""

    name: str  # schema-qualified
    estimated_rows: int | None  # the stored estimate; None when never analyzed or vacuumed
    live_rows: int  # the running count of live rows
    modified_since_analyze: int
    analyzed: bool

    @property
    def rows(self) -> int:
        """Best guess at its size now: the stored estimate lags bulk loads, the live count
        restarts at a statistics reset."""
        return max(self.estimated_rows or 0, self.live_rows)


@dataclass(frozen=True)
class StatementPlan:
    """The generic plan of one workload statement, or why there is none. `row_lookup_only` when
    the statement is DML planned as the SELECT that finds its rows (ADR 0002): the plan leaves out
    the write itself, triggers and index maintenance."""

    plan: PlanNode | None
    row_lookup_only: bool = False
    skipped: str | None = None


@dataclass(frozen=True)
class UnindexedForeignKey:
    """A foreign key whose columns no index starts with: deleting or updating a referenced row
    scans the whole table."""

    table: CollectionRef
    constraint: str
    columns: list[str]


@dataclass(frozen=True)
class ScanActivity:
    """How a table has been read since statistics were reset."""

    table: CollectionRef
    seq_scans: int
    seq_rows_read: int
    idx_scans: int
    live_rows: int


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

    def in_scope(self, analyzer: AnalyzerName) -> set[str]:
        """Qualified names of the collections `analyzer` measured in this Run."""
        return {r.qualified for r in self.scope.get(analyzer, [])}


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
SettableStatus = Literal["open", "acknowledged", "fixed"]  # obsolete is set by Runs only
Severity = Literal["info", "low", "medium", "high"]


def is_fact(severity: Severity) -> bool:
    """Whether an Observation states a fact (a table among the largest) rather than a problem.
    Facts are never covered, so a Run that stops seeing one never asks "fixed?", and Run
    comparison and summaries leave them out of the problems."""
    return severity == "info"


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
    rule: str | None = None  # when one subject can have several Findings of this category
    collection: str | None = None  # the collection the subject is or belongs to, if any
    covered_by: AnalyzerName | None = None  # set by the Run, from the analyzer's declaration

    @property
    def fingerprint(self) -> str:
        return ":".join(p for p in (self.category, self.subject, self.rule) if p)

    @property
    def is_fact(self) -> bool:
        return is_fact(self.severity)


@dataclass(frozen=True)
class Finding:
    """A problem or fact about one subject, with an identity stable across Runs.
    `unobserved_by` is the latest Run whose scope covered the subject but did not observe the
    Finding: it asks the engineer "fixed?". It is never answered automatically."""

    connection_id: str
    fingerprint: str
    category: FindingCategory
    subject: str
    status: FindingStatus
    first_seen_run: str
    last_seen_run: str
    unobserved_by: str | None = None
    collection: str | None = None  # the collection the subject is or belongs to, if any
    # The analyzer whose scope decides whether a Run's silence about this Finding means
    # something, as the latest Run to observe it recorded. None for facts, and for categories no
    # analyzer covers yet: those never ask "fixed?".
    covered_by: AnalyzerName | None = None


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

    @property
    def is_fact(self) -> bool:
        return is_fact(self.severity)


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


class UnknownCollections(Exception):
    """Collections asked for by name that the Connection does not have."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(
            f"unknown tables: {', '.join(names)}; use schema-qualified names as "
            "get_storage_stats returns them"
        )
        self.names = names


class ConnectionRefused(Exception):
    """The target cannot be analysed safely or is unsupported."""


class QueryRejected(Exception):
    """A statement was refused before reaching the server."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PrivacyRejected(QueryRejected):
    """The privacy filter refused SQL the LLM wrote: an output would carry row data."""


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
