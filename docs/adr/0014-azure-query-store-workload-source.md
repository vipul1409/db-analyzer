# Azure Query Store as a second workload source

**Status:** accepted (#17). Verified against fixture tables mimicking `query_store.qs_view`; a live Azure check is deferred until access exists.

**Query Store is read only when `pg_stat_statements` can't be.** It is the second entry in the workload sources. It counts as readable on Azure flexible server (the probe sees `azure.extensions`) when `pg_qs.query_capture_mode` is not `none` and the role has `CONNECT` on `azure_sys`. The probe reads Query Store's settings with `current_setting(…, true)`, because `pg_settings` need not list them. With neither source, the enable steps are still those for `pg_stat_statements`.

**The auxiliary session is a `SafeExecutor` the Connection's executor opens.** `SafeExecutor.auxiliary("azure_sys")` opens a session with the Connection's DSN, changing only the database name, and runs the same read-only self-check and hardening (`open_session`). The executor it returns shares the Connection id, Thread, audit sink, query cap and EXPLAIN gate limits. Its audit entries carry the purpose suffix `(azure_sys)`. Nothing is stored for it, so it never appears as a Connection.

**The read is gated.** Unlike `pg_stat_statements`, `query_store.qs_view` is not exempt from the EXPLAIN gate. It is an ordinary view over tables, and exempting it by name would also exempt any user table called `query_store.qs_view` in agent SQL. On a busy server with a long retention, the gate may refuse the read, and the Run then says so (below).

**The read defaults to the last 14 days, and the window is configurable.** Query Store keeps one row per `query_id` per recording interval, so a long retention can push the planner's row estimate past the gate's `max_result_rows` (default 10,000) — the common cause of a refused read. The workload Run therefore adds `AND q.start_time >= now() - make_interval(secs => N)` to the read, defaulting to the last 14 days (`DEFAULT_WORKLOAD_WINDOW`). This both scopes the ranking to recent traffic and cuts the row estimate the gate checks. The window is `RunOptions.workload_window`: a value of `None` reads all of retention. It is surfaced as `--workload-window-days` on the CLI and `workload_window_days` in the HTTP API and web UI (omitted is the 14-day default; `0` reads all of retention). `pg_stat_statements` keeps cumulative counters, not per-interval rows, so the window does not apply to it and is ignored.

**The read keeps this database's statements by other roles, and none of Azure's own.** The filter is `db_id` = the target database's oid, `user_id` ≠ the analyzer role, and `NOT is_system_query`. Rows are one per interval, so they are summed per `query_id` in SQL, and the analyzer merges them by fingerprint as before (ADR 0010).

**Values are replaced by placeholders in the adapter.** Query Store keeps the text of each statement's first run, literals included. Before anything ranks, stores or shows the text, each literal becomes `$n`, numbered after any existing placeholder, as `pg_stat_statements` does (pglast's scanner; a negative number is one value). This keeps evidence placeholder-only, so the privacy filter's guarantee holds. Text that can't be scanned (cut inside a literal), and text at `pg_qs.max_query_text_length` (default 6000 bytes, so cut off and possibly parseable as a different statement), is replaced by a marker. The analyzer counts that marker as unparseable and never reads it further.

**The stats window starts at the oldest interval held.** `stats_reset` is the earliest `start_time` read, in UTC, so the window is bounded by the shorter of `pg_qs.retention_period_in_days` and `workload_window` (above). The interval still being recorded isn't visible yet. The report's label is now "statistics since", which reads right for both sources.

**A source that looked readable but fails to read refuses the ranking.** A denied `SELECT`, a gate refusal or a refused auxiliary session leaves the workload unranked, with the reason as the first warning, and the Run is partial (ADR 0010). This applies to `pg_stat_statements` as well. The query cap still ends the Turn.

## Considered Options

- **A second Connection for `azure_sys`:** rejected by the proposal (§5.3). It would show up as a target and split the audit log.
- **Exempt `qs_view` from the gate like `pg_stat_statements`:** rejected for the reason above.
- **Use `query_type` to drop utility statements in SQL:** not needed. Values are replaced first, and the analyzer already leaves out and counts utility statements.
- **Fall back to the schema-only review when Query Store can't be read:** deferred. A refused read says why, which is more useful than a review that hides the misconfiguration.
- **Raise `max_result_rows` instead of windowing the read:** rejected. It would weaken the gate for every statement to work around one read, and still rank the whole retention period rather than recent traffic. A default window fixes the row estimate at its source and scopes the ranking.
- **Shrink `pg_qs.retention_period_in_days` on the server to bound the read:** rejected. It permanently discards history for everyone and is a server change the analyzer shouldn't require; the per-Run window leaves retention untouched.
