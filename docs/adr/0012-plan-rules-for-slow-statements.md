# Slow statements explained by plan rules on their generic plans

**Status:** accepted (#15); amends ADR 0002 on the guard profile and the shape of the DML rewrite

A workload Run plans every ranked statement (ADR 0002: `EXPLAIN (GENERIC_PLAN, VERBOSE)` on PG 16+, `PREPARE` and `EXPLAIN EXECUTE` with NULLs on PG 15) and runs six plan rules on the normalized tree. The rules that fire become the `slow_query` Observation's explanation. Each one quotes the plan node it is about, and the evidence holds the plan as text, up to 40 nodes.

**The rewritten DML runs under the internal profile.** ADR 0002 said the rewritten SELECT could go through the agent profile. It could, but the profile follows the code path, never the SQL: this is verbatim workload text, and the PG 15 path needs `PREPARE`, which only the internal profile accepts. The guard still never accepts EXPLAIN or PREPARE of DML.

**The UPDATE rewrite keeps its SET values.** `UPDATE t SET a = $1 WHERE id = $2` becomes `SELECT a = $1 FROM t WHERE id = $2`, not `SELECT 1 …`. A placeholder used only in SET would otherwise have no type, and PREPARE and GENERIC_PLAN both refuse it. `DEFAULT` and multi-column assignments are dropped, so a statement whose placeholder is used only there isn't planned and says so. `INSERT … SELECT` is planned as its SELECT. `INSERT … VALUES` (which finds no rows) and `MERGE` are not planned. Every DML Observation starts by saying the plan covers only how the statement finds its rows.

**The PG 15 sequence runs in one transaction, and the cleanup always runs.** `SafeExecutor.execute_sequence` checks every statement with the guard before running any of them. It runs them in one read-only transaction that is rolled back, then runs `DEALLOCATE` of a unique name on its own, because a prepared statement outlives the rollback. When the sequence fails, the cleanup may fail too, since nothing was prepared. That failure is audited, and the sequence's own error is raised. When the plan succeeds but the cleanup fails, the error is raised, so a leaked prepared statement is never silent.

**A statement that cannot be planned is a skipped measurement.** The guard refused a function, the role can't read a table, or a placeholder has no type. The Finding keeps its ranking, with `plan_skipped` and the reason in its evidence, and the Run is not partial.

**The rules read estimates, so their thresholds are about what the planner expects.** "Large" means at least 10,000 rows, taken as the larger of `reltuples` and the live-row counter, since either one lags. "Selective" means at most 5% of the rows. "Non-selective" means at least 10% returned through an index condition, or a filtered walk of a whole index. A nested loop is "big" from 1,000 outer rows and 100,000 rows in all. A sort or hash is reported as spilling only when the statement wrote temporary files, because a generic plan can't show a spill. Staleness uses the inventory's stale-statistics test. When rules fire, they replace the hints that ranking guessed from the statistics alone.

## Considered Options

- **One Finding per rule (`slow_query:<hash>:<rule>`):** rejected. The statement is the subject (ADR 0010), and a rule that stops firing after an index is created would ask "fixed?" about a statement that is still slow for another reason.
- **Plan with real values from the workload source:** rejected. Normalized text has none, and plans built from them would vary from Run to Run.
- **Compare a sort's size to `work_mem`:** deferred. The probe reads the analyzer's own capped `work_mem`, not the application's.
