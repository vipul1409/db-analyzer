# DB Analyzer

A conversational agent that analyses a PostgreSQL database through a read-only login. See `docs/proposal.md` for the design, `docs/execution-plan.md` for the plan and `CONTEXT.md` for the domain glossary.

## Usage

Create the read-only login with `docs/setup/postgres_role.sql` (Azure notes in `docs/setup/azure.md`), then:

```sh
export DBX_DSN='postgresql://db_analyzer:<secret>@host:5432/app'
uv run dbx connect prod                     # probe the database and show what the analyzer can see
uv run dbx connect prod --alias-identifiers # optional: the LLM sees schema/table/column names as aliases
uv run dbx analyze prod --report out.md     # inventory Run (no LLM): sizes, schema totals, ranked Findings
uv run dbx analyze prod --table public.orders --exact-counts   # optional: count(*) where the EXPLAIN gate allows
uv run --env-file .env dbx chat prod        # chat with the agent (needs OPENAI_API_KEY)
uv run --env-file .env dbx chat prod --thread <id>   # resume a conversation, even after a restart
```

`dbx chat` streams the answer and shows each tool call and every SQL statement as it runs, including those of the subagent it delegates to: "Analyse storage" goes to the inventory-analyst, which answers with Findings ranked by severity (bloat, stale statistics, tables with more index than heap, oversized TOAST). Beyond its dedicated tools, the agent may write its own read-only SQL; any statement whose output could carry row values (`SELECT email …`) is rejected before it runs, so the LLM sees only metadata, aggregates and entity keys. With `--alias-identifiers`, names reach the LLM only as aliases (`table_3`) and `dbx chat` shows the real ones. Each conversation is a Thread bound to one Connection; the agent's model is `DBX_MODEL` (default `gpt-5.4-mini`). A turn stops cleanly at 200k tokens or 60 tool calls.

Each `analyze` is recorded as a Run, with its Findings, in the local store. Sizes, row estimates and vacuum/analyze recency come from the catalog; a partitioned table is summed over its partitions. Two measurements read table data and run only where allowed: exact counts (opt-in) and, for tables whose counters suggest bloat, a `pgstattuple_approx` scan. One that is refused is listed with its reason, and the table keeps its estimate (ADR 0007). A Finding seen again in a later Run gains an Observation instead of being duplicated. Every SQL statement passes the guard (read-only allowlist) and, unless it reads only the catalog, the EXPLAIN gate (cost and row limits), and is written to the audit log.

To try it on the synthetic dataset (see Development):

```sh
make db-up PG=pg17 && make db-seed PG=17
export DBX_DSN=postgresql://db_analyzer:db_analyzer@localhost:5417/shop
uv run dbx connect shop && uv run dbx analyze shop --report out.md
```

Secrets come from the environment. Copy `.env.example` to `.env` (git-ignored), fill in `OPENAI_API_KEY`, and load it with `uv run --env-file .env …`.

The DSN stays in the environment; only the variable name is stored (in `~/.db-analyzer`, or `$DBX_HOME`).

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync                     # create the virtualenv
uv run pre-commit install   # lint and typecheck on commit
make check                  # lint, types, unit tests
make db-up                  # start Postgres 14–18 fixtures (ports 5414–5418; 14 tests refusal)
make test-integration       # integration tests against the fixtures
make db-down                # stop and remove the fixtures
```

### Synthetic dataset

`make db-seed PG=17` creates a `shop` database on that fixture: a multi-tenant schema with seeded problems (a hot tenant, unindexed foreign keys, duplicate/unused/invalid indexes, bloat, stale statistics, a tenant-partitioned table, and an index-heavy link table in a second schema, `reference`) and a replayed workload in `pg_stat_statements`. `tests/fixtures/dataset/ground_truth.json` lists the Findings it should produce. Integration tests seed it automatically at CI scale (about a second per version).

`make db-seed PG=17 SCALE=full` seeds roughly 5–10 GB for manual and scale testing; expect it to take several minutes.

Hot standbys for PG 15 and 16 (ports 5515/5516) are behind a compose profile: `make db-up PG="pg15 pg16 pg15-standby pg16-standby"`. Spikes (`tests/spikes`) are run by hand with `uv run pytest -m spike -s`.

Start a subset of fixtures with `make db-up PG="pg15 pg16"` and point the integration tests at them with `DBX_TEST_PG_VERSIONS=15,16`.

### Safety suites

`tests/safety/corpus.py` lists about 270 statements the analyzer must never run. `make check` asserts the guard rejects all of them; `make test-integration` runs the ones the read-only role should also refuse, with the guard bypassed (ADR 0004). The same file lists read-only statements whose output carries row data: the guard accepts them, and `make check` asserts the privacy filter rejects them (ADR 0006).

The guard's function allowlist is built from `src/db_analyzer/safety/pg_catalog.json`, a snapshot of PG 15–18. When adding a Postgres version, start its fixture and regenerate it with `uv run python -m tests.fixtures.catalog_snapshot`; an integration test fails while it is out of date.

### Agent tests and LangGraph Studio

Agent tests replay recorded model responses from `tests/cassettes/` (one file per turn), so CI needs no API key or network; tools and SQL still run against the PG 17 fixture. After changing prompts, tools or the model, re-record them:

```sh
DBX_LLM_CASSETTE_MODE=record DBX_TEST_PG_VERSIONS=17 uv run --env-file .env \
  pytest tests/integration/test_agent_chat.py tests/integration/test_cli_chat.py
```

To debug the agent graph in LangGraph Studio, add a Connection with `dbx connect` and start the dev server with that Connection's name:

```sh
DBX_STUDIO_CONNECTION=shop uv run langgraph dev    # DBX_DSN must point at the database
```
