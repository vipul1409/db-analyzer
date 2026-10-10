# Generic plans for normalized statements: go, with DML planned as SELECT

**Status:** accepted (spike #4); amended by ADR 0012 (guard profile, shape of the DML rewrite)

Normalized `pg_stat_statements` text (`$n` placeholders) can be planned read-only as `db_analyzer`:

- **PG 15:** `SET LOCAL plan_cache_mode = force_generic_plan`, `PREPARE`, `EXPLAIN (FORMAT JSON) EXECUTE` with one `NULL` per placeholder, then `DEALLOCATE`.
- **PG 16+:** `EXPLAIN (GENERIC_PLAN, FORMAT JSON)`.

Both work inside a read-only transaction on a primary and on a hot standby, and they produce the same plans on PG 16.

**DML can't be EXPLAINed by the analyzer role.** EXPLAIN checks table privileges, and `db_analyzer` has no UPDATE/DELETE grant (the startup self-check refuses any role that has one). Every path fails with `permission denied` on every version, primary and standby. As a superuser the same read-only EXPLAIN works, which confirms the privilege is the only cause.

**Decision:** plan the row-finding part of a DML statement. Rewrite `UPDATE/DELETE … WHERE c` (keeping `FROM`/`USING`) to `SELECT 1 FROM <target> … WHERE c` with the same parameters, and plan that. The rewritten text is a plain SELECT, so it goes through the agent guard profile unchanged. The guard never has to accept EXPLAIN of DML in any profile.

## Considered Options

- **Grant UPDATE/DELETE to the analyzer:** rejected. It breaks read-only by construction, and the self-check would refuse the role.
- **Report DML without plans:** rejected. Slow UPDATE/DELETE statements are usually slow because of the row search, and the rewrite recovers exactly that part.

## Consequences

- The internal guard profile in the proposal (§3.3) no longer needs EXPLAIN/PREPARE around DML. It still needs `PREPARE`, `EXPLAIN EXECUTE`, `DEALLOCATE` and `SET LOCAL plan_cache_mode` for the PG 15 path.
- Plans for DML don't include the `ModifyTable` node, trigger cost or the cost of index maintenance. Explanations of DML Findings must say the plan covers row lookup only.
- A statement prepared before an error **survives the rollback**. The PG 15 path must use a unique statement name per call and `DEALLOCATE` it in all cases (or `DEALLOCATE ALL` when the session is reused).
- With `pg_stat_statements.track = all`, the analyzer's own statements (including `PREPARE … AS <workload text>`) are recorded too. The workload provider must exclude rows where `userid` is the analyzer role, or it will rank and re-wrap its own statements.
- Evidence: `tests/spikes/test_generic_plans.py` (`make db-up PG="pg15 pg16 pg15-standby pg16-standby"`, `make db-seed PG="15 16"`, `uv run pytest -m spike -s tests/spikes`).
