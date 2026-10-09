# Guard allowlist from a catalog snapshot; the corpus records which layer stops each statement

**Status:** accepted (#8)

**Function allowlist.** Proposal §3.3 sketches a short named allowlist (size functions, `pgstattuple_approx`, HypoPG). Agent SQL needs far more than that: aggregates, string, date, JSON and window functions. So QueryGuard allows every `pg_catalog` function that is stable or immutable, minus a reviewed denylist, plus a short list of harmless volatile functions and the named extension functions (`safety/functions.py`). Stable and immutable functions change nothing by contract. The denylist covers the ones that still bypass the other layers: `table_to_xml`/`schema_to_xml`/`database_to_xml` read whole tables past the EXPLAIN gate and the privacy filter, `ts_rewrite` runs SQL text, and `txid_current`/`pg_current_xact_id` assign transaction IDs. Every volatile function is refused unless listed, which covers sleeps, advisory locks, backend signalling, `set_config`, sequences, large objects, file access and `query_to_xml`.

The names come from `safety/pg_catalog.json`, a snapshot of PG 15–18 (`uv run python -m tests.fixtures.catalog_snapshot`). An integration test fails when a supported server has a function or relation the snapshot lacks.

**Names, not resolution.** The guard parses but does not resolve. An unqualified call is taken to be the `pg_catalog` function of that name. A same-named function or operator planted in a user schema, or one hidden behind a view or a cast, is invisible to it. The read-only transaction and role still refuse any write such a function attempts.

**Corpus layers.** The acceptance criterion "with the guard bypassed, every corpus statement fails at the database" cannot hold. Running the corpus showed that the read-only role and the rolled-back read-only transaction accept many statements:
- `pg_sleep` and the advisory locks;
- `pg_terminate_backend`, which only warns;
- `set_config` and `SET`;
- `NOTIFY`/`LISTEN`, `COMMIT` and `SET TRANSACTION READ WRITE` before the first query;
- `PREPARE` of DML, and `EXPLAIN` of `CREATE TABLE AS`;
- `pgstattuple`, which reads a whole table.

So each corpus entry records its layer:
- **database:** the role or transaction refuses it. The database suite runs these with the guard bypassed and asserts the refusal's SQLSTATE (read-only transaction, insufficient privilege, transaction block or syntax), so a statement cannot pass by failing for an unrelated reason such as a missing object.
- **guard:** only the guard stops it. These run in the guard suite only.

**Gate exemption.** Statements that execute nothing (`EXPLAIN`, `SHOW`, `PREPARE`, `DEALLOCATE`, `SET LOCAL`) are exempt. So are SELECTs whose relations are all catalog relations, after resolving CTE names by scope, with no recursive CTE, and no row-producing or relation-reading function outside a short catalog list. The EXPLAIN gate cannot price CPU-bound expressions or catalog cross joins. `statement_timeout` bounds those.

## Considered Options

- **A hand-written function allowlist:** rejected. It would be thousands of names, or so few that ordinary analytical SQL fails.
- **Resolving names against the live catalog** (`pg_proc` lookups per statement): deferred. It closes the planted-function gap, but it couples the guard to a live connection and makes it untestable without a database.
- **Dropping guard-only statements from the corpus:** rejected. They are exactly the cases the guard exists for.

## Consequences

- A new Postgres major version needs the snapshot regenerated before it is supported.
- The guard accepts `SET LOCAL plan_cache_mode`, but SafeExecutor runs each statement in its own rolled-back transaction. The generic-plan path (#15) needs a multi-statement transaction in SafeExecutor.
- The per-Turn query cap (`QueryBudget`) is in SafeExecutor. The agent creates one per Turn (#9).
