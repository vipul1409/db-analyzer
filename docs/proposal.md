# DB Analyzer: Implementation Proposal

| | |
|---|---|
| **Status** | Draft v3 for review (v2: LLM moved to OpenAI API. v3: domain terms defined in `CONTEXT.md`; Run/Finding/Observation, guard profiles, EXPLAIN-gate scope, forced hashing of text entity keys) |
| **Author** | Vipul Modi (drafted with Claude) |
| **Date** | 9 Oct 2026 |
| **v1 scope** | PostgreSQL 15+ (self-managed and Azure Database for PostgreSQL – Flexible Server) |
| **LLM** | OpenAI API (direct); provider abstraction kept for later vendors |
| **Next stores** | MySQL / other SQL engines, then KV stores and search indices |

---

## 1. Goals and non-goals

### 1.1 Goals

A conversational "deep agent" that connects to a database with a **read-only** user, explores it systematically, and answers in depth on:

1. **Inventory:** row count and size (heap, indexes, TOAST, total) of every table, rolled up by schema and by partition parent.
2. **Slow queries and missing indexes:** finds the most expensive statements, explains why they are slow, proposes indexes and, where HypoPG exists, shows the estimated cost improvement.
3. **Read-only by construction:** never runs DML/DDL; needs only a read-only role.
4. **EXPLAIN before every data read:** every statement that reads relation data is first EXPLAINed and rejected if its estimated cost or row count exceeds configured limits. Statements that only touch catalogs or settings are exempt by statement type (§3.4).
5. **Entity hotspots:** reads the schema to work out which columns are **entity keys** (identify an entity type such as tenant, account, booking…) and reports which **entities** take a disproportionate share of rows and bytes. Terms are defined in `CONTEXT.md`.

### 1.2 Non-goals for v1

- Applying any fix (creating indexes, VACUUM, config changes). The agent only recommends and outputs DDL as text.
- Continuous monitoring or alerting. Runs happen on demand from chat; the SQLite history supports comparing runs.
- Multi-user hosted SaaS, SSO, RBAC. v1 is a single-user local web app.
- Comparing Connections (e.g. "prod vs staging"). A Thread belongs to exactly one Connection for its whole life.
- Query rewriting beyond advice.

### 1.3 Decisions captured from Q&A

| Topic | Decision |
|---|---|
| Language / framework | Python + LangGraph / `deepagents` |
| LLM | **OpenAI models via the OpenAI API directly** (api.openai.com). No local model runtime. The model factory stays provider-agnostic so other vendors can be added later, but OpenAI is the only provider built and tested in v1 |
| Interaction | **Chat / interactive** through a **web chat UI** |
| Postgres hosting | Self-managed + Azure |
| Postgres versions | **15+** |
| Slow-query source | **Auto-detect** what is available and degrade gracefully |
| "Too complex" gate | **EXPLAIN cost + row thresholds** (configurable) + timeouts |
| Hotspot measurement | **Exact counts, only when the EXPLAIN gate allows**; skipped otherwise |
| Entity-column discovery | **LLM + foreign-key graph**, user can confirm/override in chat |
| Hotspot metric | **Both** rows and bytes |
| Data visible to the LLM | **Metadata + entity IDs only**; no row contents. Text-typed entity keys are always hashed (ADR 0001) |
| Connection scope | One **Connection** = one target database. Auxiliary sessions (e.g. `azure_sys`) belong to it and are never separate Connections |
| Run | A **Run** = one execution of one or more analyzers over a recorded scope (analyzers × collections), broad or targeted; follow-ups that only explain collected data do not create Runs |
| Finding identity | A **Finding** is stable across Runs (fingerprint = category + subject, rules in §4.1); each Run adds an **Observation** of it. Status lives on the Finding |
| Entity-ID hashing | HMAC-SHA256 with a per-Connection local secret (§3.5) |
| Run scope vs skips | Scope lists measured collections only; gate-skipped ones are recorded separately and make the Run `partial` |
| Finding lifecycle | `open` → `acknowledged` / `fixed` (user) or `obsolete` (automatic, when the subject can no longer exist) |
| Thread | Bound to one Connection for life; no cross-Connection comparison in v1 |
| Index validation | **HypoPG if available**, else plan-based reasoning |
| Typical target | < 100 GB, < 500 tables |
| Target node | Primary or replica, whichever DSN is given |
| Next store | MySQL / other SQL |
| Persistence | **Local SQLite** for findings, snapshots and chat threads |

---

## 2. Architecture overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  Web chat UI  (streams tokens, tool progress, tables, findings)       │
└───────────────▲──────────────────────────────────────────────────────┘
                │ SSE / WebSocket
┌───────────────┴──────────────────────────────────────────────────────┐
│  API server (FastAPI)                                                │
│   • threads / runs / connections  • report export  • audit log view  │
└───────────────▲──────────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────────────┐
│  Agent layer (LangGraph + deepagents)                                 │
│   Orchestrator ─┬─ inventory-analyst                                  │
│   (planning,    ├─ workload-analyst                                   │
│    todo list,   ├─ index-advisor                                      │
│    synthesis)   └─ hotspot-analyst                                    │
│   LLM: OpenAI API via provider-agnostic model factory                 │
└───────────────▲──────────────────────────────────────────────────────┘
                │ capability-scoped tools (generated per connection)
┌───────────────┴──────────────────────────────────────────────────────┐
│  Analysis core (store-agnostic)                                       │
│   Analyzers: Inventory · Workload · IndexAdvice · Hotspot             │
│   Domain model: Collection, StorageStats, WorkloadItem, Plan,         │
│                 Run, Finding, Observation                             │
└───────────────▲──────────────────────────────────────────────────────┘
                │ Adapter interface + capability flags
┌───────────────┴──────────────────────────────────────────────────────┐
│  Store adapters (plugins)                                             │
│   postgres (v1) │ mysql (next) │ redis │ elasticsearch │ …            │
│   each = Probes + Catalog queries + Planner(EXPLAIN) + QueryGuard     │
└───────────────▲──────────────────────────────────────────────────────┘
                │ ALL access goes through ↓
┌───────────────┴──────────────────────────────────────────────────────┐
│  SafeExecutor: parse → allowlist → EXPLAIN gate → timeouts → result   │
│  filter (privacy) → audit log                                         │
└──────────────────────────────────────────────────────────────────────┘
          Local SQLite: chat checkpoints · snapshots · findings · audit
```

**Design principles**

1. **Deterministic core, LLM on top.** Collection, measurement and plan analysis are plain Python returning structured results. The LLM plans, picks what to dig into, interprets and explains. This keeps numbers exact (they never come from the model), keeps token usage and cost low, and makes the system testable.
2. **Safety outside the model.** Read-only and cost limits are enforced in code and in the database role, never only in the prompt.
3. **Capabilities, not store types.** Analyzers and tools ask "does this adapter support `QUERY_WORKLOAD`?", not "is this Postgres?". A new store implements the capabilities it can and the agent sees only those tools.
4. **Normalized domain model.** Every adapter maps its native concepts (table, keyspace prefix, index/shard) into the same few types, so reports, persistence and UI don't change per store.

---

## 3. Safety model (read-only + EXPLAIN gate)

Safety is layered; any single layer failing still leaves the others.

### 3.1 Layer 1: database role

A dedicated login with no write privileges. Provided as a setup script (`docs/setup/postgres_role.sql`):

```sql
CREATE ROLE db_analyzer LOGIN PASSWORD '…'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE db_analyzer SET default_transaction_read_only = on;
ALTER ROLE db_analyzer SET statement_timeout = '30s';
ALTER ROLE db_analyzer SET lock_timeout = '1s';
ALTER ROLE db_analyzer SET idle_in_transaction_session_timeout = '60s';

GRANT pg_monitor TO db_analyzer;          -- pg_stat_*, pg_stat_statements text, settings, pgstattuple_approx
GRANT CONNECT ON DATABASE app TO db_analyzer;
GRANT USAGE ON SCHEMA public TO db_analyzer;           -- per schema
GRANT SELECT ON ALL TABLES IN SCHEMA public TO db_analyzer;  -- needed only for hotspot counts
```

- `SELECT` on application tables is **optional**. Without it, inventory, workload and index advice still work from catalogs/statistics; hotspot analysis is reported as "unavailable: no SELECT privilege".
- On Azure Flexible Server, `pg_monitor` is grantable by the admin user; extensions (`pg_stat_statements`, `hypopg`, `pgstattuple`) must be allow-listed in `azure.extensions` and created by an admin. The agent never tries to create them; it only detects them.

### 3.2 Layer 2: session hardening

Every connection opened by the executor runs:

```sql
SET default_transaction_read_only = on;
SET statement_timeout = '<cfg>';  SET lock_timeout = '1s';
SET application_name = 'db-analyzer';
SET work_mem = '<cfg, e.g. 32MB>';   -- avoid large memory spikes
```

and each statement runs inside `BEGIN READ ONLY … ROLLBACK`. Startup self-check: the agent attempts a harmless write probe (`CREATE TEMP TABLE`) inside a rolled-back transaction **only to verify it fails**; if it succeeds, the session aborts with "role is not read-only". (Temp tables are blocked by read-only transactions, so this is a safe check.)

### 3.3 Layer 3: SQL parsing and allowlist (`QueryGuard`)

Every statement, whether from a vetted template or written by the LLM, is parsed with **`pglast`** (the real Postgres parser) before it reaches the server.

The guard has two **profiles**; the caller cannot choose its own:

| Profile | Used by | Difference |
|---|---|---|
| **Agent** | `run_readonly_sql` and any SQL text that came from the LLM | `EXPLAIN` may wrap only a `SELECT` that would itself pass the guard. No `PREPARE` / `EXECUTE` / `DEALLOCATE`, no `SET LOCAL` |
| **Internal** | Vetted templates and the generic-plan path (§5.3) | `PREPARE` / `EXPLAIN EXECUTE` / `DEALLOCATE` and `SET LOCAL plan_cache_mode`, wrapping only SELECTs taken verbatim from the workload source (`pg_stat_statements` / Query Store) or produced by the DML-to-SELECT rewrite, never text the LLM wrote or edited. EXPLAIN never wraps DML in any profile (ADR 0002) |

Allowed (both profiles unless noted):

- A single top-level `SELECT` (incl. CTEs that are themselves pure `SELECT`; no data-modifying CTEs).
- `EXPLAIN` **without** `ANALYZE` (ANALYZE executes the statement), subject to the profile rule above.
- `SHOW`; `SET LOCAL` of an allowlisted GUC set (e.g. `plan_cache_mode`): internal profile only.
- `PREPARE` / `EXPLAIN EXECUTE` / `DEALLOCATE`: internal profile only, generic-plan path (§5.3).
- Function calls on an allowlist: catalog/size functions (`pg_total_relation_size`, `pg_relation_size`, `pg_indexes_size`, `pg_column_size`, …), `pgstattuple_approx`, and HypoPG's `hypopg_create_index`, `hypopg_reset`, `hypopg_relation_size` (these create backend-local, in-memory hypothetical indexes; nothing is written to disk or catalogs).

Rejected: everything else, including `SELECT … FOR UPDATE/SHARE`, `SELECT INTO`, `COPY`, `DO`, `CALL`, `LOCK`, `VACUUM`, `ANALYZE`, `pg_sleep`, `dblink*`, `lo_*`, `pg_read_file`, `set_config` outside allowlist, `nextval`, `pg_terminate_backend`, `pg_cancel_backend`, any unknown function in a user schema (could have side effects).

### 3.4 Layer 4: EXPLAIN gate

The gate applies to every permitted statement that **reads relation data** (templates included). Statements exempt by type, listed explicitly in the guard so the exemption cannot widen silently:

- `EXPLAIN` itself (it plans, it does not execute);
- `SHOW` and `SET LOCAL`;
- `PREPARE` / `DEALLOCATE`, and `hypopg_*` calls;
- `SELECT`s whose `FROM` touches only `pg_catalog` / `information_schema` relations and statistics views, or calls only allowlisted catalog/size functions, with no user relation in the query.

The exemption is decided from the parse tree, not from where the statement came from. For gated statements:

1. Run `EXPLAIN (FORMAT JSON, COSTS ON) <stmt>`.
2. Extract `Total Cost`, top-level `Plan Rows`, and the maximum `Plan Rows` of any scan node.
3. Reject if any limit is exceeded:

```yaml
safety:
  max_total_cost: 2_000_000       # planner cost units
  max_result_rows: 10_000         # rows returned to the agent
  max_scan_rows: 50_000_000       # rows any single scan may touch
  statement_timeout: 30s
  lock_timeout: 1s
  max_queries_per_turn: 50        # runaway-loop guard
```

4. On rejection, the tool returns a structured reason (`"cost 8.4e6 > 2e6"`) so the agent can try a cheaper route (e.g. statistics instead of a count) or tell the user it skipped the step.

Defaults are tuned for < 100 GB databases and are per-connection overridable from the UI.

### 3.5 Layer 5: result filter (privacy)

The LLM may see **metadata, aggregates and entity IDs only**. Enforcement:

- Vetted templates declare their output columns; only those are passed through.
- LLM-written SQL is checked by `pglast` before execution: every output expression must be an aggregate (`count`, `sum`, `avg`, `min`, `max` of sizes/lengths) or a column that the hotspot analyzer has marked as an **entity key**. Anything else (e.g. `SELECT email …`) is rejected before it runs.
- `pg_stats.most_common_vals` and `pg_stat_statements.query` can contain literals. The query text is normalized (constants already become `$n` in `pg_stat_statements`); `most_common_vals` is only exposed for entity-key columns.
- **Entity keys of a surrogate type** (integer types, `uuid`) pass through in clear unless `privacy.hash_entity_ids: true`. **Entity keys of any other type** (text, varchar, citext, composite…) are **always hashed** before they reach the LLM, whatever the config says, because natural keys are often personal data (email, phone, username). The UI un-hashes for display locally. See ADR 0001.
- Optional `privacy.hash_entity_ids: true` extends hashing to surrogate-type entity keys as well.
- **Hash function:** HMAC-SHA256 keyed with a per-Connection secret, truncated to 12 hex chars. A plain hash would be reversible by enumeration (all integers up to a few million, short slugs). The secret is generated when the Connection is created and stored locally with the credentials (OS keychain, or a `0600` key file in v1), never in SQLite, never sent to the LLM. The same secret is used for every Run, so hashed IDs stay comparable across Runs.

**What leaves the network.** Because the LLM is the OpenAI API, everything the agent sends to the model goes to OpenAI: schema metadata (table/column names, types, FKs), statistics, normalized query text from `pg_stat_statements`, plan excerpts, aggregates and entity IDs (or their hashes). Row contents never do, because the filter above runs before any tool result is handed to the model. Controls:

- A single `LLMGateway` is the only code path that calls OpenAI. It re-applies the privacy filter to every outgoing message (defence in depth) and logs a redacted copy of each request/response to SQLite for review.
- `privacy.redact_identifiers: false` (optional) replaces schema/table/column names with stable aliases for organisations that treat schema names as sensitive; the UI maps them back for display.
- OpenAI API data controls (data retention, training opt-out, zero-data-retention eligibility) must be confirmed for the organisation's account before connecting production databases (§12.2).

### 3.6 Layer 6: audit log

Every statement (or rejection) is written to SQLite: timestamp, thread, tool, SQL, EXPLAIN cost/rows, decision, duration, row count. Viewable in the UI as an "SQL ran in this session" panel.

---

## 4. Extensibility model

### 4.1 Domain model (store-agnostic)

Terms used below (Connection, Run, Finding, Observation, Entity type, Entity key, Entity, Entity map, Hotspot) are defined in `CONTEXT.md`.

```python
class CollectionKind(StrEnum):
    TABLE = "table"; PARTITIONED_TABLE = "partitioned_table"; MATVIEW = "matview"
    KEY_PREFIX = "key_prefix"          # KV stores
    SEARCH_INDEX = "search_index"      # Elasticsearch / OpenSearch
    COLLECTION = "collection"          # document stores

@dataclass
class CollectionRef:
    store_id: str; namespace: str | None; name: str; kind: CollectionKind

@dataclass
class StorageStats:
    ref: CollectionRef
    row_count: int | None; row_count_method: Literal["exact","estimate","sample"]
    data_bytes: int; index_bytes: int; toast_bytes: int | None; total_bytes: int
    extra: dict[str, Any]              # bloat %, dead tuples, shard count, memory usage…

@dataclass
class WorkloadItem:
    fingerprint: str; text: str; calls: int
    total_ms: float; mean_ms: float; rows: int
    io: dict[str, float]; source: str  # "pg_stat_statements" | "azure_query_store" | "log"

@dataclass
class PlanNode:  # normalized plan tree, adapter fills from native EXPLAIN
    op: str; relation: CollectionRef | None; est_rows: float; est_cost: float
    filter: str | None; index: str | None; children: list["PlanNode"]

AnalyzerName = Literal["inventory","workload","index_advice","hotspot"]

@dataclass
class Run:
    id: str; connection_id: str; thread_id: str | None
    scope: dict[AnalyzerName, list[CollectionRef] | None]  # measured only; None = all collections
    skipped: dict[AnalyzerName, list[tuple[CollectionRef, str]]]  # planned but not measured, with reason
    started_at: datetime; finished_at: datetime | None
    status: Literal["running","complete","partial","failed"]

@dataclass
class Finding:  # stable identity across Runs
    fingerprint: str                   # category + subject, e.g. "unused_index:app.events.ix_events_foo"
    category: Literal["size","slow_query","missing_index","unused_index",
                      "duplicate_index","invalid_index","bloat","stale_stats",
                      "hotspot","config"]
    subject: CollectionRef | str       # collection, index name, query fingerprint, entity…
    status: Literal["open","acknowledged","fixed","obsolete"]
    first_seen_run: str; last_seen_run: str

@dataclass
class Observation:  # what one Run saw for one Finding
    run_id: str; finding: str          # Finding.fingerprint
    severity: Literal["info","low","medium","high"]
    title: str; evidence: dict; recommendation: str | None; ddl: str | None
```

- A **Run** is created whenever one or more analyzers execute end to end, whether broad ("give me a full analysis", "analyse storage") or targeted ("which tenant uses the most space in `events`?" = a hotspot Run scoped to `events`). Its **scope** records which analyzers ran and over which collections were **actually measured**. Collections the EXPLAIN gate (or a missing privilege) kept out go in `skipped` with the reason, are not in scope, and make the Run `partial`. Follow-up questions that only explain or drill into data already collected ("why is query #3 slow?") reuse the latest Run's data and do not create a Run.
- Run-to-Run comparison only compares data for scope the two Runs share (same analyzer, overlapping collections), so partial or targeted Runs never look like regressions.
- **Fingerprint rules** (`category:subject`), chosen so the same problem keeps the same Finding across Runs, PG upgrades and primary/replica switches:

| Category | Subject |
|---|---|
| `slow_query` | hash of the normalized query text (not `queryid`, which changes across major versions and OID differences) |
| `missing_index` | table + proposed column list in order, e.g. `app.bookings(tenant_id,created_at)` |
| `unused_index`, `duplicate_index`, `invalid_index` | schema-qualified index name |
| `size`, `bloat`, `stale_stats` | schema-qualified collection name (+ rule name when a collection can have several, e.g. `toast_oversized`) |
| `hotspot` | entity type + entity ID (hashed when §3.5 says so) + collection |
| `config` | setting name |
- When a Run that covers a Finding's analyzer no longer observes it, the Finding is flagged "not seen in latest run: fixed?" for the user to confirm. It is never moved to `fixed` automatically. An `acknowledged` Finding stays acknowledged when it is observed again.
- A Finding becomes **obsolete** automatically when its subject can no longer exist: the collection or index is gone from the latest probe, its entity key was removed from the entity map, or the Connection's hash secret was rotated (hashed entity IDs change). No user judgement is needed, so no prompt. Obsolete Findings are hidden from the findings board by default.

### 4.2 Adapter interface

```python
class Capability(StrEnum):
    INVENTORY = "inventory"                    # sizes / counts per collection
    SCHEMA_GRAPH = "schema_graph"              # columns, PK/FK, stats
    QUERY_WORKLOAD = "query_workload"          # top statements
    PLAN_EXPLAIN = "plan_explain"
    HYPOTHETICAL_INDEX = "hypothetical_index"
    INDEX_HEALTH = "index_health"              # unused/duplicate/bloat
    ENTITY_DISTRIBUTION = "entity_distribution"
    KEYSPACE_SCAN = "keyspace_scan"            # KV
    SHARD_STATS = "shard_stats"                # search

class StoreAdapter(Protocol):
    kind: ClassVar[str]                        # "postgres"
    def connect(self, cfg: ConnectionConfig) -> None: ...
    def probe(self) -> ProbeResult: ...        # version, extensions, privileges, host type
    def capabilities(self) -> set[Capability]: ...
    def guard(self) -> QueryGuard: ...         # dialect parser + allowlist + EXPLAIN gate
    # capability-specific providers; return None if unsupported
    def inventory(self) -> InventoryProvider | None: ...
    def schema(self) -> SchemaProvider | None: ...
    def workload(self) -> WorkloadProvider | None: ...
    def planner(self) -> PlannerProvider | None: ...
    def index_advisor(self) -> IndexAdvisorProvider | None: ...
    def distribution(self) -> DistributionProvider | None: ...
```

- Adapters register through Python entry points (`db_analyzer.adapters`), so a new store is a separate package: `pip install db-analyzer-mysql`.
- `probe()` runs first on every connection and decides capabilities dynamically (e.g. `HYPOTHETICAL_INDEX` only if `hypopg` is installed; `QUERY_WORKLOAD` sources ranked by availability).
- **Tool generation:** the agent's tool list is built from `capabilities()`. If a connection lacks a capability, its tools don't exist, so the LLM can't call them and the report states the gap and how to enable it.

### 4.3 How other stores map

| Concept | PostgreSQL (v1) | MySQL (next) | Redis / KV | Elasticsearch / OpenSearch |
|---|---|---|---|---|
| Collection | table / partition | table | key prefix / pattern | index / data stream |
| Size | `pg_total_relation_size` | `information_schema.TABLES` | `MEMORY USAGE` on sampled keys | `_cat/indices`, `_stats` |
| Workload | `pg_stat_statements`, Azure Query Store, logs | `performance_schema.events_statements_summary_by_digest` | `SLOWLOG GET`, `INFO commandstats` | slow log, `_nodes/stats` |
| Plan | `EXPLAIN (FORMAT JSON)` | `EXPLAIN FORMAT=JSON` | n/a | `profile: true` on bounded query |
| Gate | cost + rows | `query_cost` + rows_examined | command allowlist (`SCAN` w/ COUNT, no `KEYS`), max keys sampled | allowlist of read APIs, `size`/`terminate_after` caps |
| Entity hotspot | `GROUP BY tenant_id` | `GROUP BY tenant_id` | key-prefix histogram (`tenant:{id}:*`) | `terms` agg on routing/tenant field, per-shard doc counts |
| Read-only | role + read-only txn | `SELECT`-only user + `transaction_read_only` | ACL user with `+@read -@write -@dangerous` | role with `read`, `monitor`, `view_index_metadata` |

Because MySQL is next, the SQL-specific pieces (catalog-template runner, EXPLAIN gate, FK graph, `GROUP BY` hotspot logic) live in a shared `sql_common` package; the Postgres and MySQL adapters supply dialect templates and a plan normalizer. The parser for MySQL guard would be `sqlglot` (no `pglast` equivalent).

---

## 5. PostgreSQL adapter (v1)

### 5.1 Probe

Collected once per connection and shown to the user as "what I can see":

- `server_version_num` (refuse < 150000), `in_recovery` (`pg_is_in_recovery()`: replica or primary), host type (Azure detected via `azure.extensions` GUC / `azure_sys` database presence).
- Installed extensions: `pg_stat_statements`, `hypopg`, `pgstattuple`, `pg_buffercache`.
- Privileges: membership in `pg_monitor` / `pg_read_all_stats`; `has_table_privilege(…,'SELECT')` per table.
- Settings that affect advice: `track_io_timing`, `pg_stat_statements.track`, `log_min_duration_statement`, `random_page_cost`, `shared_buffers`, `work_mem`.
- Statistics freshness: `pg_stat_statements_info.stats_reset`, `pg_stat_database.stats_reset`, last `analyze` per table (stale stats make every estimate below less reliable; agent warns).

### 5.2 Inventory (requirement 1)

| Metric | Source | Cost |
|---|---|---|
| Heap / index / TOAST / total bytes | `pg_relation_size`, `pg_indexes_size`, `pg_total_relation_size`, `reltoastrelid` | catalog only |
| Estimated rows | `pg_class.reltuples`, `pg_stat_user_tables.n_live_tup` | catalog only |
| Exact rows | `SELECT count(*)` | **only if EXPLAIN gate passes**, opt-in per table or "all small tables" |
| Dead tuples / bloat | `n_dead_tup`, `pgstattuple_approx` (if installed, gated) | low |
| Partition rollup | `pg_inherits` / `pg_partition_tree` | catalog only |
| Vacuum/analyze recency | `pg_stat_user_tables` | catalog only |

Output: top-N tables by total size, schema rollups, estimate-vs-exact flag per row count, and findings for bloat, stale stats, oversized TOAST, tables with more index bytes than heap.

### 5.3 Workload and slow queries (requirement 2)

**Source auto-detection, in order:**

1. `pg_stat_statements` (self-managed and Azure, if installed and readable). Rank by `total_exec_time`, `mean_exec_time`, `shared_blks_read`, `temp_blks_written`; keep top N (default 25) by each and dedupe.
2. **Azure Query Store** (`azure_sys.query_store.qs_view`) when on Azure and `pg_stat_statements` is missing. Requires an **auxiliary session** to the `azure_sys` database: opened internally with the Connection's credentials and safety settings, it goes through the same `SafeExecutor`, is audited under the same Connection, and is never shown or managed as a separate Connection.
3. **Log files** (self-managed only, optional): the user uploads or points to a log file in the UI; parsed offline with a `log_min_duration_statement` / `auto_explain` parser. The agent never reads server files through SQL (`pg_read_file` is blocked).
4. **None available:** the agent says so, gives the exact steps to enable option 1, and continues with a schema-only index review (FKs without indexes, sequential-scan-heavy tables from `pg_stat_user_tables.seq_scan / seq_tup_read`).

**Getting plans for normalized queries on PG 15.** `pg_stat_statements` stores `$1, $2…` placeholders. `EXPLAIN (GENERIC_PLAN)` only exists from PG 16, so the adapter uses two paths:

- **PG 16+:** `EXPLAIN (GENERIC_PLAN, FORMAT JSON) <query>`.
- **PG 15:** within a read-only transaction (internal guard profile, §3.3): `SET LOCAL plan_cache_mode = force_generic_plan; PREPARE q AS <query>; EXPLAIN (FORMAT JSON) EXECUTE q(NULL, …); DEALLOCATE q;` (number of NULLs from the parameter count; a unique name per call, deallocated even on error, because a prepared statement survives rollback).

DML statements found in `pg_stat_statements` (e.g. slow `UPDATE`s) are **never executed or EXPLAINed directly**: EXPLAIN checks table privileges and the read-only role has no UPDATE/DELETE grant. Instead `UPDATE/DELETE … WHERE c` is rewritten to `SELECT 1 FROM <target> … WHERE c` and that row-finding part is planned (ADR 0002). The workload provider excludes the analyzer role's own statements (`userid`), which `track = all` would otherwise record.

**Plan analysis (deterministic rules)** over the normalized plan tree:

- Seq Scan on a large relation with a selective filter.
- Nested Loop with a large estimated outer side.
- Sort / Hash with large estimated input (likely `work_mem` spill; confirmed by `temp_blks_written`).
- Filter on a column with an FK and no supporting index.
- Index scan followed by a large filter (index not selective, composite index candidate).
- Large row estimates with stale `last_analyze`.

Each rule produces a `Finding` with the plan excerpt as evidence; the LLM turns it into an explanation.

### 5.4 Index advice (requirement 2)

1. **Candidate generation** from plan rules: columns in `WHERE` equality/range predicates, join keys, `ORDER BY` + `LIMIT`; composite order = equality columns first, then range, then sort. Also FK columns without a leading-column index.
2. **Dedupe** against existing indexes (prefix coverage, `pg_index.indkey`).
3. **Validation with HypoPG** (when installed): `hypopg_create_index('CREATE INDEX ON t (a, b)')`, re-run the generic-plan EXPLAIN, record cost before/after and whether the planner picks the index, then `hypopg_reset()`. Report the estimated index size with `hypopg_relation_size`.
4. **Without HypoPG:** advice is marked "unvalidated (plan-based reasoning)", with lower confidence.
5. **Index health** (bonus, cheap): unused indexes (`idx_scan = 0` since stats reset, excluding PK/unique), duplicate/overlapping indexes, invalid indexes (`indisvalid = false`).
6. Output includes ready-to-copy `CREATE INDEX CONCURRENTLY …` text. The agent never runs it.

### 5.5 Entity hotspots (requirement 5)

**Step 1: find entity columns (LLM + FK graph).**

- Build the FK graph from `pg_constraint`. Root candidates = tables with high FK fan-in (many tables reference them), e.g. `tenants`, `accounts`, `bookings`.
- Candidate columns in other tables = FK columns pointing at roots, plus name patterns (`tenant_id`, `org_id`, `account_id`, `customer_id`, `*_uuid` on roots).
- Signal from `pg_stats` (metadata only): `n_distinct`, `most_common_freqs` for those columns. A column where one value has `most_common_freqs` > 5% is a strong hotspot candidate before any table read.
- The LLM receives this compact graph (table names, column names, types, FK edges, size, n_distinct) and proposes an **entity map**: which columns are entity keys for which entity type, and the hierarchy between entity types (booking → account → tenant). Each proposed entity key carries its column type, so the UI shows whether its values will be hashed (§3.5). The proposal is shown in chat for the user to confirm or edit; the confirmed map is saved per connection in SQLite and reused.

**Step 2: measure (exact, gated).** For each (table, entity column) in the confirmed map, ordered by table size:

```sql
-- rows
SELECT tenant_id, count(*) AS rows
FROM app.bookings GROUP BY tenant_id ORDER BY rows DESC LIMIT 20;

-- bytes (approximation of on-disk share; includes TOAST'd values uncompressed)
SELECT tenant_id, count(*) AS rows, sum(pg_column_size(b.*)) AS bytes
FROM app.bookings b GROUP BY tenant_id ORDER BY bytes DESC LIMIT 20;
```

- Each query goes through the EXPLAIN gate. Bytes queries are heavier (they touch whole rows incl. TOAST), so they run only when the rows query passed and the bytes query passes on its own.
- If the gate rejects, the step is recorded as **"skipped: exceeds limits (cost X)"**, and the `pg_stats` signal from Step 1 is reported as an *estimate* so the user still gets a hint. The user can raise limits for that table from chat.
- **Roll-up through the hierarchy:** for child tables that only reference a mid-level entity (e.g. `booking_items.booking_id`), a join to the parent to attribute to `tenant_id` is attempted, also gated.
- Partitioned tables: if partitioned by the entity key, sizes come from partition sizes directly (no scan).

**Step 3: report.** Per entity: share of rows and bytes per table and across the database, top-N entities, Gini/skew indicator, and contextual findings (e.g. "tenant 4821 = 38% of `events` bytes; consider per-tenant retention or a partition by tenant").

### 5.6 Postgres catalog query library

All fixed queries live as versioned `.sql` files under `adapters/postgres/queries/` with a small header declaring output columns, minimum version and required privilege. This keeps them reviewable, testable against each supported version, and selectable by the probe.

---

## 6. Agent design

### 6.1 Graph

Built with `deepagents` (LangGraph underneath):

- **Orchestrator:** owns the conversation, writes a todo plan for broad requests ("analyse this database"), delegates to subagents, merges their findings into a single answer, and handles follow-ups ("why is query #3 slow?", "what about tenant 4821 in `audit_log`?").
- **Subagents** (isolated context, own tool subset, own short prompt):

| Subagent | Tools | Output |
|---|---|---|
| `inventory-analyst` | `probe`, `list_collections`, `get_storage_stats`, `count_rows_exact` | `StorageStats[]`, findings |
| `workload-analyst` | `get_top_queries`, `explain_query`, `analyze_plan` | ranked `WorkloadItem[]` + plan findings |
| `index-advisor` | `suggest_indexes`, `validate_index_hypothetical`, `index_health` | index findings with DDL |
| `hotspot-analyst` | `get_schema_graph`, `propose_entity_map`, `measure_entity_distribution` | hotspot findings |

- Shared tool: `run_readonly_sql(sql, purpose)` for ad-hoc questions, which goes through the full `SafeExecutor` incl. the privacy filter.
- deepagents' virtual filesystem holds large intermediate results (e.g. full inventory JSON) so they don't flood the context; tools return summaries + a file handle. No shell or real-filesystem tools are given to the agent.

### 6.2 Tool contract

Tools return compact, pre-digested JSON (top-N, already ranked, units normalized) rather than raw catalog rows. This keeps prompts small (lower latency and token cost) and improves accuracy: the model reasons over "top 10 tables with sizes" far more reliably than over 500 raw rows. Large results stay in the deepagents virtual filesystem and are paged in only when needed.

### 6.3 LLM provider: OpenAI

```yaml
llm:
  provider: openai              # only provider implemented in v1
  model: <primary model>        # orchestrator; chosen by the eval suite (§9), see §12.2
  api_key_env: OPENAI_API_KEY   # key read from env / secret store, never from config files
  temperature: 0                # ignored by models that don't support it
  reasoning_effort: medium      # only for reasoning models that support it
  subagent_overrides:           # optional: smaller, cheaper model for narrow subagents
    inventory-analyst: { model: <small model> }
    workload-analyst:  { model: <small model> }
  limits:
    max_tokens_per_turn: 200_000      # hard stop per user message, across all subagents
    max_tool_calls_per_turn: 60
    request_timeout_s: 120
    max_retries: 4                    # exponential backoff on 429 / 5xx
  budget:
    monthly_usd_cap: <set by owner>   # UI warns at 80%, refuses new runs at 100%
```

- **Client:** `langchain-openai` (`ChatOpenAI`), created through LangChain's `init_chat_model`. Model names are config values, not code, so moving to a newer OpenAI model is a config change qualified by the eval suite.
- **Tool calling:** native OpenAI function calling with **strict JSON schemas** for every tool, so tool arguments are always valid against the Pydantic models in §4.1. Parallel tool calls are enabled for independent read-only tools (e.g. sizing several tables).
- **Structured outputs:** the entity-map proposal (§5.5) and final findings summary use OpenAI structured outputs against a Pydantic schema, so the UI renders them without parsing free text.
- **Token and cost tracking:** every call's token usage is recorded per thread/run in SQLite and shown in the UI (tokens and estimated cost per run). Limits above stop runaway loops.
- **Rate limits:** a shared async semaphore caps concurrent OpenAI requests (default 4) so parallel subagents don't trip org rate limits; 429s back off and retry.
- **Prompt caching:** system prompts and tool schemas are kept stable and placed first so OpenAI's automatic prompt caching applies across turns, reducing cost on long chats.
- **Provider abstraction kept, not exercised:** all model access goes through `agent/llm_factory.py` and the `LLMGateway`. Adding another vendor later means adding a factory branch plus a `ModelProfile` (context window, tool-call limits, structured-output support) and passing the eval suite. No other code depends on OpenAI-specific types.
- **No local runtime:** Ollama, GPU sizing and local-model prompts are out of scope. The only network dependency added is outbound HTTPS to `api.openai.com`.

### 6.4 Interaction flow (example)

1. User adds a connection in the UI → probe runs → agent replies with "what I can see" (version, primary/replica, extensions, missing privileges, stats age).
2. User: "Give me a full analysis." → orchestrator plans todos → subagents run (progress streamed) → summary with top findings; details in expandable sections.
3. Agent proposes the entity map → user confirms/edits → hotspot analysis runs.
4. Follow-ups drill down; every SQL is visible in the audit panel.
5. "Export report" → Markdown/JSON of the current findings.

---

## 7. Persistence (local SQLite)

| Table | Purpose |
|---|---|
| `connections` | name, adapter kind, DSN reference (secrets in OS keychain / env, not in SQLite), safety overrides. One row = one target database |
| `probes` | probe result per connection, taken at connect and at the start of each Run |
| `runs` | `Run` rows: connection, thread, scope (analyzers × measured collections), skipped collections + reasons, timing, status |
| `snapshots` | `StorageStats` and `WorkloadItem` per Run, for trend comparison ("`events` grew 12 GB since last week") |
| `findings` | `Finding` rows keyed by fingerprint per connection; status (`open`, `acknowledged`, `fixed`, `obsolete`), first/last seen Run |
| `observations` | `Observation` rows: one per Finding per Run that saw it; severity, evidence, recommendation, DDL |
| `entity_maps` | confirmed entity map per connection, versioned |
| `audit_log` | every statement / rejection (§3.6) |
| LangGraph checkpoints | `SqliteSaver` for chat threads, so conversations survive restarts |

SQLAlchemy models + Alembic migrations so the store can later move to Postgres without changes to callers.

---

## 8. Web chat UI

**Recommended:** FastAPI backend with SSE streaming, and a small React (Vite) front-end. Panels:

- Chat with streamed answers and inline tables/sparklines.
- Run progress (todo list from the orchestrator, subagent status).
- Findings board (filter by category/severity, mark acknowledged).
- Audit panel (SQL, EXPLAIN cost, accepted/rejected).
- Connection manager + safety-limit editor.

Faster alternative for an MVP: LangGraph's open-source Agent Chat UI / LangGraph dev server, then replace with the custom UI once the findings board and audit panel are needed. (Choice listed in §12.)

---

## 9. Testing strategy

| Area | Approach |
|---|---|
| **Safety (highest priority)** | Corpus of ~200 forbidden statements (DML, DDL, data-modifying CTEs, `FOR UPDATE`, `COPY`, `DO`, side-effect functions, multi-statement strings, comment/quoting tricks) must all be rejected by `QueryGuard` under the **agent** profile; the same corpus (minus verbatim-workload `EXPLAIN`/`PREPARE` of DML) must be rejected under the **internal** profile, and `EXPLAIN <DML>` must be rejected under the agent profile. Gate-exemption cases (catalog-only `SELECT`s) and non-exempt look-alikes (catalog view joined to a user table) are in the corpus too. Second test: same corpus executed with guard disabled against a test DB using the real role must fail at the DB layer. Both run in CI. |
| EXPLAIN gate | Fixture DB with known sizes; assert accept/reject at threshold boundaries. |
| Catalog queries | Run every `.sql` template against Docker Postgres **15, 16, 17, 18**. |
| Generic-plan path | PG 15 PREPARE path vs PG 16+ GENERIC_PLAN on the same workload, incl. DML statements. |
| Analyzers | Seeded DB (pgbench + a synthetic multi-tenant schema with deliberate skew, missing FK indexes, bloat, duplicate indexes); assert expected findings. |
| HypoPG | With and without the extension installed. |
| Azure | Integration run against a small Azure Flexible Server (manual / nightly) for Query Store and extension detection. |
| Agent evals | Scripted conversations with expected findings/tool calls, scored per OpenAI model (accuracy, tool-call validity, tokens, cost, latency) to choose the primary and subagent models and to qualify any model upgrade. LLM calls are recorded/replayed (VCR-style cassettes) so CI runs offline and free; a live eval run is triggered manually or nightly. |

---

## 10. Delivery plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **0. Spikes (≈1 wk)** | PG 15 generic-plan path incl. DML; HypoPG on replica and Azure; OpenAI tool calling + structured outputs with deepagents (shortlist 2–3 models, measure tokens/cost per full analysis); `pglast` guard prototype | Go/no-go notes per spike |
| **1. Core + safety (≈2 wks)** | Domain model, adapter interface, `SafeExecutor` (all 6 layers), Postgres probe, SQLite store, safety test corpus | Safety suite green on PG 15–18 |
| **2. Inventory + workload (≈2 wks)** | Inventory analyzer, workload sources (pg_stat_statements, Azure Query Store), plan normalizer + rules | Correct findings on seeded DB |
| **3. Index advice (≈1 wk)** | Candidate generation, HypoPG validation, index health | Expected indexes suggested with cost deltas |
| **4. Hotspots (≈1.5 wks)** | FK graph, entity-map proposal + confirmation, gated exact counts, roll-up | Seeded skew detected; skips reported correctly |
| **5. Agent + UI (≈2 wks)** | deepagents graph, subagents, web chat UI, audit/findings panels, export | End-to-end chat on seeded and one real DB |
| **6. Hardening (≈1 wk)** | Model eval suite, cost/usage dashboard, budget limits, docs, role setup scripts | Eval passes on the chosen OpenAI models within the agreed cost per analysis |
| **7. MySQL adapter (next)** | `sql_common` extraction proved by second adapter | Inventory + workload + hotspots on MySQL 8 |

Estimates assume one engineer; adjust once spikes are done.

---

## 11. Proposed repository layout

```
db-analyzer/
├── pyproject.toml
├── src/db_analyzer/
│   ├── core/            # domain model, capabilities, findings, config
│   ├── safety/          # SafeExecutor, gate, privacy filter, audit
│   ├── analyzers/       # inventory, workload, index_advice, hotspot (store-agnostic)
│   ├── adapters/
│   │   ├── sql_common/  # template runner, FK graph, plan normalizer base
│   │   └── postgres/    # probe, queries/*.sql, guard (pglast), planner, hypopg
│   ├── agent/           # deepagents graph, subagent prompts, tool factory, llm factory, LLMGateway
│   ├── store/           # SQLAlchemy models, alembic migrations
│   └── api/             # FastAPI app, SSE
├── web/                 # React chat UI
├── docs/setup/          # postgres_role.sql, azure.md, enabling pg_stat_statements/hypopg
└── tests/
    ├── safety/          # forbidden-statement corpus
    ├── fixtures/        # docker-compose PG 15–18, seed scripts
    └── evals/           # scripted agent conversations
```

---

## 12. Risks and open questions

### 12.1 Risks

| Risk | Mitigation |
|---|---|
| Model hallucinates findings or numbers | Deterministic analyzers produce findings; LLM only explains. Every number in an answer must come from a tool result (system-prompt rule + eval check). Model qualification suite. |
| Database metadata sent to a third party (OpenAI) | Privacy filter + `LLMGateway` re-check; optional ID hashing and schema-name aliasing; request log for review; confirm OpenAI data-retention terms before production use. |
| Token cost grows with large schemas / long chats | Pre-digested tool results, virtual filesystem for large data, smaller model for subagents, prompt caching, per-turn and monthly limits. |
| OpenAI outage, rate limits or model deprecation | Retries with backoff; clear UI error (DB analysis never depends on the model for safety); model names in config; eval suite re-run before switching models. |
| Planner estimates are wrong (stale stats) → gate lets a heavy query through or blocks a cheap one | `statement_timeout` as hard backstop; warn when `last_analyze` is old; per-table override in UI. |
| `pg_stat_statements` reset recently → misleading workload | Show `stats_reset` age prominently; refuse to rank if window < configurable minimum (e.g. 1 h). |
| Running against a busy primary | Low `work_mem`, single connection, `statement_timeout`, gated queries; UI shows "connected to PRIMARY" banner. |
| `pg_column_size(row)` misstates on-disk bytes for TOAST/compressed data | Label as "logical bytes"; compare with table-level TOAST size for context. |
| Literal values leaking via query text or stats | Normalized query text only; `most_common_vals` exposed only for entity keys; optional ID hashing. |

### 12.2 Open questions (to settle before Phase 1)

1. **OpenAI models:** which models are allowed for the orchestrator and subagents? Proposal: shortlist 2–3 current models in Phase 0 and pick by eval score and cost per analysis.
2. **Data governance with OpenAI:** is sending schema metadata, normalized query text and entity IDs to the OpenAI API approved? Does the account need zero data retention, and should `hash_entity_ids` / schema-name aliasing be on by default?
3. **Budget:** target cost per full analysis and monthly cap?
4. **Web UI:** custom React UI from the start, or LangGraph Agent Chat UI for the MVP?
5. **Multi-database scope:** one database per connection, or should a connection cover all databases on a server?
6. **Schema-per-tenant layouts:** do any target DBs model tenants as schemas rather than columns? (Changes hotspot logic to schema-level size rollups.)
7. **Hotspot fallback:** when exact counts are skipped, is showing the `pg_stats`-based estimate (clearly labelled) acceptable, or should the step be reported as skipped only?
8. **Secrets:** where should DB credentials and the OpenAI API key live: OS keychain, `.env`, or an existing vault (e.g. Azure Key Vault)?
9. **Log-file source:** is uploading Postgres logs through the UI wanted for self-managed servers, or is pg_stat_statements/Query Store enough?
10. **Who uses it:** only engineers who can read all IDs, or also people who should see hashed IDs only? (Decides the default for `hash_entity_ids` on surrogate-type keys; text-typed keys are always hashed per ADR 0001.)
