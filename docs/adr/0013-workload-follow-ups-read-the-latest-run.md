# Workload follow-ups read the latest Run through the workload-analyst

**Status:** accepted (#16)

"What's slow?" goes to the workload-analyst subagent, which calls `get_top_queries`: a workload Run, digested into statements numbered by rank (most total time first) with the plan rules that fired. A follow-up about one of them ("why is query #3 slow?") also goes to the workload-analyst, which calls `get_query_details(rank)`. That tool reads the latest workload Run from the store and sends nothing to the database, so a follow-up is never a Run (CONTEXT.md).

**The `explain_query` and `analyze_plan` tools of `docs/proposal.md` (§6.1, the subagent table) are not built.** A workload Run already plans every ranked statement and runs the plan rules (ADR 0012), so a separate EXPLAIN tool would only repeat that work on the database, and let the model re-plan a statement under a different guard path. The plan and the rules that fired are in the Run's evidence; `get_query_details` returns them.

**"Latest" is the Thread's latest workload Run, else the Connection's.** Ranks are only meaningful against the ranking the user saw, which is the Thread's. A fresh Thread can still ask about a ranking `dbx analyze -a workload` made.

**Follow-ups go through the subagent, not an orchestrator tool.** The workload tools stay with the subagent that owns them, as the inventory tools do. Giving the orchestrator `get_query_details` too would change its toolset, and with it every recorded cassette's request shape.

**The agent's minimum stats window is the service's.** A deterministic Run takes `min_stats_window` per call; Turns take it from `AnalyzerService(min_stats_window=…)`, default one hour, so the model can never lower it.

## Considered Options

- **Re-rank on every follow-up:** rejected. It makes a Run per question, loads the database, and changes the numbers between the ranking and the answer.
- **Answer follow-ups from the subagent's report in the orchestrator's context:** rejected. The report holds a summary, not the plan, so the orchestrator would have nothing to quote.
