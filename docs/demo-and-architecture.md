# DB Analyzer: end-to-end demo and agent architecture

Part 1 is a demo of about 20 minutes, run on the synthetic `shop` database. It covers the CLI, the web UI and chat. Part 2 explains how the agent behind it is built. The glossary is in `CONTEXT.md`, and the reasons for each decision are in `docs/adr/`.

This document describes the code as of M9 (web UI on the v1 API). Index advice, the entity map, hotspots and full-analysis orchestration are paused (ADR 0015), so the demo leaves them out.

---

## Part 1: Demo script

### What the audience should take away

1. The agent finds real problems in a database: bloat, stale statistics, bad indexes and slow statements. Each slow statement comes with the plan node that explains it.
2. It cannot harm the database or leak its data. Every statement is read-only, cost-gated and audited, and row values never reach the LLM.
3. Findings persist across Runs, so a fix can be confirmed rather than assumed.
4. The CLI, the web UI and chat all show the same data, because they share one facade and one local store.

### Prepare (at least an hour before)

The workload analyzer refuses to rank statistics younger than one hour, and chat cannot lower that limit. So seed the database **at least an hour before the demo**, or the "what's slow?" step only shows the refusal.

```sh
uv sync && npm --prefix web ci
make db-up PG=pg17 && make db-seed PG=17      # PG 17 fixture on :5417, seeded `shop` + workload
cp .env.example .env                          # add OPENAI_API_KEY (chat only)

export DBX_HOME="$(mktemp -d)"                # a clean local store for the demo
export DBX_DSN=postgresql://db_analyzer:db_analyzer@localhost:5417/shop
```

`db_analyzer` is the read-only login created by `docs/setup/postgres_role.sql`. Keep `DBX_HOME` and `DBX_DSN` exported in every terminal used below. The API reads the DSN from its own environment.

Open three terminals: **A** for CLI commands, **B** for `uv run --env-file .env dbx serve`, and **C** for `make web`. Open http://localhost:5173 in a browser.

**Without an OpenAI key:** everything except chat still works. To show chat anyway, replay a recorded Turn by setting `DBX_LLM_CASSETTE=tests/cassettes/test_cli_chat` and asking "What are the biggest tables?". Tools and SQL run live, but the model's text is the recording, so its numbers can differ slightly from the live ones. Use this only as a fallback.

### Act 1: Connect and see what the analyzer can see (2 min)

```sh
uv run dbx connect shop
```

- **Show:** the probe output, with the server version, primary or replica, extensions (`pg_stat_statements`, `pgstattuple`), privileges and statistics age.
- **Say:** "Only the *name* of the environment variable is stored, never the DSN. On connect it also tries a write, and if that write succeeds it refuses to continue."

### Act 2: A deterministic inventory Run, no LLM (3 min)

```sh
uv run dbx analyze shop --report out.md
```

Open `out.md`. Point out these Findings, which are listed in `tests/fixtures/dataset/ground_truth.json`:

| Finding | What was seeded |
|---|---|
| `bloat:public.audit_log` | autovacuum off, every row updated twice; measured with `pgstattuple_approx` |
| `stale_stats:public.legacy_imports` | analyzed at 100 rows, then bulk-loaded |
| `size:reference.sku_categories:index_heavy` | link table whose indexes outweigh the heap |
| `duplicate_index:public.idx_accounts_tenant_dup` | same columns as `idx_accounts_tenant` |
| `unused_index:public.idx_accounts_created_at` | never scanned |
| `invalid_index:public.idx_bookings_status_unique` | a failed `CREATE UNIQUE INDEX CONCURRENTLY` |

- **Say:** "Each index Finding comes with the `DROP INDEX CONCURRENTLY` statement, as text for you to copy. The analyzer never runs DDL."
- **Optional:** `uv run dbx analyze shop --table public.events --table public.tenants --exact-counts` shows exact `count(*)` where the EXPLAIN gate allows it, and the reason where it refuses.

### Act 3: Rank the workload and explain it from plans (3 min)

```sh
uv run dbx analyze shop -a workload --report workload.md
```

- **Show:** the top statements ranked by total time, each with the plan rules that fired, quoting the plan node:
  - `FROM bookings WHERE account_id = $1`: a sequential scan for a few rows, filtered on an unindexed foreign key;
  - `FROM events ORDER BY k`: a sort that spilled to disk;
  - `UPDATE bookings … WHERE tenant_id = $1`: the plan covers the row lookup only, because writes are planned as the SELECT that finds their rows;
  - `FROM legacy_imports WHERE source = $1`: estimates built on stale statistics.
- **Say:** "Nothing was executed. These are generic plans (`EXPLAIN (GENERIC_PLAN)`, or `PREPARE` on PG 15), so no parameter values were needed."
- **If it was seeded under an hour ago:** the Run refuses to rank and says why. Show that refusal, then re-run with `--min-stats-window 0`.

### Act 4: Findings persist, and a fix is confirmed, not assumed (3 min)

Fix one problem as the superuser. This stands in for the team acting on the advice; the analyzer itself cannot do it:

```sh
docker compose -f tests/fixtures/docker-compose.yml -p db-analyzer-fixtures \
  exec pg17 psql -U postgres -d shop -c 'ANALYZE public.legacy_imports'
uv run dbx analyze shop
uv run dbx findings shop
```

- **Show:** `stale_stats:public.legacy_imports` is now marked **fixed?**. The latest Run measured that table, and the covering analyzer no longer saw the Finding. Every other Finding gained an Observation and was not duplicated.
- Confirm it with `uv run dbx fixed shop stale_stats:public.legacy_imports`, and acknowledge one with `uv run dbx ack shop bloat:public.audit_log`.
- `uv run dbx runs shop`, then `uv run dbx compare shop <older> <newer>`. The comparison covers only the scope both Runs share, so a narrower Run never looks like a regression.

### Act 5: The web UI (4 min)

In the browser at http://localhost:5173:

1. **Connections:** `shop` is already there, because the CLI and the API share `DBX_HOME`. Open it to see the probe and "how to let it see more" hints.
2. **Runs:** the three Runs from the CLI. Open the workload Run, then compare two inventory Runs and download a report as Markdown or JSON.
3. **Analyze:** start an inventory Run from the dialog, with no chat and no LLM. It appears in the list.
4. **Findings:** filter by category or severity, open one to see its Observation history, and copy its DDL. Obsolete Findings are hidden by default.

### Act 6: Chat, with the progress panel (5 min)

In **Chat**, start a Thread and ask, in order:

| Ask | What to point at in the progress panel |
|---|---|
| "Analyse storage" | `subagent_started: inventory-analyst` → `get_storage_stats` → each catalog statement with its plan cost against the gate limit → `run_finished`. The answer ranks the same Findings as Act 2. |
| "What's slow?" | delegated to the `workload-analyst`; `get_top_queries` records a new workload Run. |
| "Why is query #3 slow?" | `get_query_details` only. **No SQL runs and no Run is created**, because it reads the latest Run again (ADR 0013). |
| "Show me the emails of the 5 newest accounts" | `sql_rejected` with the privacy reason. The agent says it cannot return row values. |

Then:
- Ask something open-ended and press **Stop** mid-answer. The Turn ends as cancelled, and the Thread stays usable.
- **Audit:** every statement from this demo, from CLI, API and chat alike, with its decision, plan cost and Thread.
- **Usage:** tokens and estimated cost for the Thread and the month.

**Optional, on aliasing:** `uv run dbx connect shop2 --alias-identifiers` (same `DBX_DSN`), then chat on `shop2`. The model sees `table_3`, and the UI and CLI show the real names.

### Close (1 min)

- Recap the four takeaways.
- **Next:** packaging, e2e tests, docs, the PG 15–18 matrix and a real-database validation (M10). After that, resuming index advice and hotspots.

### Reset between rehearsals

```sh
rm -rf "$DBX_HOME" && export DBX_HOME="$(mktemp -d)"
make db-seed PG=17      # restores the seeded problems; wait an hour before the workload chat again
```

---

## Part 2: Agent architecture

### The shape in one picture

```mermaid
flowchart TB
  subgraph Fronts["Front ends"]
    CLI["CLI: dbx (typer + rich)"]
    API["HTTP API: FastAPI, SSE (dbx serve)"]
    UI["Web UI: React, generated TS client"] --> API
    Studio["LangGraph Studio"]
  end

  CLI --> SVC
  API --> SVC
  Studio --> SVC

  SVC["AnalyzerService (service.py)<br/>frozen at v1, ADR 0016"]

  SVC -->|"run(): deterministic, no LLM"| RUNS["Runs: inventory, workload analyzers"]
  SVC -->|"send(): one Turn"| AGENT["Agent graph (deepagents)<br/>orchestrator + subagents"]
  AGENT -->|tools| TB["AgentTurn (TurnBackend)"]
  TB --> RUNS
  TB -->|run_readonly_sql| EXEC
  AGENT <-->|"every model call"| GW["LLMGateway + TurnLimits + StrictTools"]
  GW <--> OAI["OpenAI (Responses API, store=false)"]

  RUNS --> EXEC["SafeExecutor<br/>cap → guard → privacy → read-only txn → EXPLAIN gate → audit"]
  EXEC --> PG[("PostgreSQL<br/>read-only login")]
  EXEC -.->|"auxiliary session"| AZ[("azure_sys<br/>Query Store")]

  SVC --> STORE[("Local store, $DBX_HOME<br/>db-analyzer.sqlite: Connections, Runs, Findings,<br/>Observations, audit, LLM requests<br/>checkpoints.sqlite: Thread state")]
```

### Layers and where they live

| Layer | Code | Responsibility |
|---|---|---|
| Front ends | `cli.py`, `api/`, `web/`, `agent/studio.py` | Rendering only. The API may import nothing behind the facade, and `tests/unit/test_architecture.py` enforces this. |
| Facade | `service.py` (`AnalyzerService`) | The only entry point. It manages Connections, Runs, Findings, Threads and Turns, audit and usage. It is frozen at v1, so a change needs an ADR (ADR 0016). |
| Agent | `agent/graph.py`, `tools.py`, `digest.py`, `llm.py`, `events.py` | The deepagents graph, its tools, the compact JSON the tools return, model middleware, and translation into `AgentEvent`s. |
| Runs | `runs/` | An `Analyzer` interface (`collect` → `Collected`) and `combine`, which turns the analyzers' results into one Run with scope, skipped collections and status. |
| Analyzers | `analyzers/` | Pure logic over collected data: inventory, index health, workload ranking, plan rules. |
| Adapter | `adapters/postgres/`, `adapters/sql_common/` | Probe, session hardening, catalog query templates (`queries/*.sql`), generic plans and the plan normalizer, and the workload sources. |
| Safety | `safety/` | Guard, EXPLAIN gate, privacy filter, identifier aliases, `SafeExecutor`. |
| Domain and store | `core/`, `store/` | The domain model (`Run`, `Finding`, `Observation` and others), the Finding lifecycle, Run comparison, and SQLite with Alembic migrations. |

### Two ways in: a Run and a Turn

**A Run** (`AnalyzerService.run`) is deterministic and needs no model:

1. The service opens a hardened, read-only session and wraps it in a `SafeExecutor`.
2. Each chosen analyzer collects through the executor and returns `Collected`: its Findings, its scope (the collections it measured), skipped collections, and snapshots.
3. `runs.combine` merges these into one outcome. The Run is partial if anything was skipped or incomplete.
4. The store upserts each Finding by fingerprint and adds an Observation per Finding.
5. A Finding whose collection the covering analyzer measured, but which it did not observe, gets `unobserved_by`. That is the "fixed?" prompt. Findings on dropped objects become obsolete (ADR 0008, ADR 0011).

**A Turn** (`AnalyzerService.send`) is one user message in a Thread:

1. The service claims the Thread, so a second concurrent Turn raises `TurnActive` (409 over HTTP).
2. It builds the graph for the Connection's capabilities. Tools exist only for capabilities the Connection has, so a database without `pg_stat_statements` or Query Store gets no workload tools.
3. It streams `agent.astream(..., subgraphs=True)`. The `Translator` turns LangGraph chunks into `AgentEvent`s. SQL and Run events from tools arrive on the custom stream.
4. The Turn always ends with `usage`, then `done`, whether it finished, failed or was cancelled. `cancel` is cooperative: it stops the stream at its next await point, and the checkpoint stays valid.

A tool that measures something calls the same Run code as `dbx analyze`. "Analyse storage" in chat and `dbx analyze` therefore produce the same kind of Run and the same Findings. The model only explains them.

### The agent graph

`agent/graph.py` builds one deepagents graph:

| Agent | Tools | Job |
|---|---|---|
| Orchestrator | `probe`, `run_readonly_sql`, `task` (delegation) | Routes questions and writes the answer. Every number in it must come from a tool result. |
| `inventory-analyst` | `probe`, `get_storage_stats`, `count_rows_exact` | Records an inventory Run and reports ranked Findings and sizes. |
| `workload-analyst` | `get_top_queries`, `get_query_details` | Records a workload Run and explains statements from plan rules. Follow-ups read the latest Run instead of ranking again. |

The graph has no shell tool and no general-purpose subagent that would inherit every tool (ADR 0003). Tools are thin: they call `AgentTurn`, which is bound to one Thread, Connection and per-Turn query cap. They return pre-digested JSON (`agent/digest.py`), never raw catalog rows. A failure comes back as an explicit error, so the model reports it rather than filling the gap.

Each model call, the orchestrator's and the subagents' alike, passes through this middleware, outermost first:

1. **`TurnLimits`:** ends the Turn cleanly at 200k tokens or 60 tool calls, with a `limit_reached` event.
2. **`StrictTools`:** strict JSON schemas on every tool bind.
3. **`LLMGateway`:**
   - re-checks everything bound for the model for personal data, and refuses to send it;
   - applies identifier aliases;
   - caps concurrent calls;
   - writes a redacted request log;
   - totals usage and cost;
   - records or replays cassettes, so CI runs offline.

The model is OpenAI through LangChain, over the Responses API with `store=false`. It defaults to `gpt-5.4-mini`, and `DBX_MODEL` overrides that.

### The safety pipeline

Every statement, whether it comes from a template, from workload text or from the LLM, goes through `SafeExecutor` (`safety/executor.py`). A lint-style test bans raw cursor use anywhere else.

```
query cap ─▶ guard (pglast) ─▶ privacy check* ─▶ BEGIN READ ONLY ─▶ EXPLAIN gate† ─▶ execute ─▶ result filter ─▶ ROLLBACK ─▶ audit
```

- **Query cap:** at most 50 statements per Turn, rejected ones included.
- **Guard:** a single statement from a read-only allowlist and a function allowlist built from a PG 15–18 catalog snapshot. There are two profiles. The **agent** profile, for LLM SQL, is stricter than the **internal** profile, for templates and verbatim workload text: for example, `EXPLAIN` of DML is internal only (ADR 0004).
- **\*Privacy check (LLM SQL only):** every output column must be an aggregate of sizes or lengths, catalog metadata, or an entity key. `SELECT email` and `avg(amount)` are refused before they run. Templates declare their output columns instead (ADR 0006).
- **†EXPLAIN gate:** a statement that reads table data runs only if its plan's cost and row estimates are within the Connection's limits. Catalog-only and settings statements are exempt by statement type.
- **Session hardening:** statement and lock timeouts, a read-only self-check at connect, and the transaction always rolled back.
- **Auxiliary session:** Azure's `azure_sys`, for Query Store, goes through the same pipeline, cap and audit, under the same Connection (ADR 0014).
- **Safety corpus:** about 270 forbidden statements in `tests/safety/corpus.py`. They are rejected by the guard in unit tests, and by the read-only role with the guard bypassed in integration tests.

### What the LLM can see

- It sees metadata, aggregates and statement text normalized to placeholders. It never sees row values.
- Query Store's literal values are replaced with placeholders before anything ranks or stores them.
- With `--alias-identifiers`, schema, table and column names reach the model as stable aliases. The `Translator` swaps the real names back in before events reach the user.
- Entity keys reach the model only once an entity map exists. That feature is paused, so today no entity key reaches it at all.

### Events: one stream for every front end

`AgentEvent` (`agent/events.py`) is a Pydantic union discriminated by `type`:

`token` · `tool_started` · `tool_finished` · `subagent_started` · `subagent_finished` · `sql_executed` · `sql_rejected` · `run_finished` · `limit_reached` · `usage` · `error` · `done`

The CLI renders them with Rich, the API sends them as SSE, and the UI's progress panel renders them live. `subagent_*` events come from the orchestrator's `task` call, and everything the subagent does streams between them.

### State

| Store | Holds |
|---|---|
| `$DBX_HOME/db-analyzer.sqlite` (default `~/.db-analyzer`) | Connections (with the DSN's variable name only), probes, Runs with scope and snapshots, Findings, Observations, the audit log, Threads, LLM request logs, identifier aliases. Alembic-managed. |
| `$DBX_HOME/checkpoints.sqlite` | LangGraph checkpoints per Thread. A Thread resumes after a restart, and `history` reads it without a model key. |
| Environment | DSNs, `OPENAI_API_KEY`, `DBX_API_TOKEN`, `DBX_MODEL`. |

### Deliberate limits in v1

- One engineer, running locally. The API binds to `127.0.0.1`, takes an optional bearer token, and refuses a non-loopback address without one.
- One database per Connection, and one Turn per Thread.
- `run` is blocking. Streaming a Run would need a new event, and so an ADR.
- Paused, with seams left open: index advice (#18), the entity map and its `resume` interrupt, entity-key hashing and hotspots (#19–#22), the budget cap (#23), full analysis (#24), evals (#25).
