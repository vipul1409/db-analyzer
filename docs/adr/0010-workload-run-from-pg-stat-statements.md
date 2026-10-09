# Workload Run: fingerprints, the stats window and the schema-only fallback

**Status:** accepted (#14)

**Ranking happens in Python.** The provider reads every row of `pg_stat_statements` for this database (at most `pg_stat_statements.max`, 5,000 by default) except the analyzer role's own (ADR 0002), then merges rows by fingerprint and ranks. SQL can't compute the fingerprint, and the same statement run by two roles or under two server-side ids must count once. The read is exempt from the EXPLAIN gate (an extension statistics view) and runs under the internal profile.

**The fingerprint is a hash of the normalized text.** `slow_query:<first 16 hex of sha256>` of the text with whitespace collapsed and no trailing semicolon, never `queryid`. The same statement keeps its Finding across PG 15, 16 and 17, which the integration suite checks.

**Only queries and DML are ranked.** Utility statements are left out and counted: PostgreSQL doesn't replace values in them before 16, so `CREATE ROLE … PASSWORD '…'` could put a secret in evidence the LLM later sees, and they have no plan to analyze. So are statements whose text is hidden (no `pg_read_all_stats`, with a warning that says how to fix it) and truncated ones. Evidence holds the placeholder text only.

**The statistics window is a Run parameter.** `min_stats_window` (default 1 hour, `--min-stats-window` hours on the CLI) is checked against `pg_stat_statements_info.stats_reset`. Younger statistics produce no ranking and a warning first in the report. A window under a day still ranks, with a warning. The integration tests lower it, since the seeded dataset resets statistics seconds before it replays. It isn't a stored Connection setting yet.

**What a ranked statement is.** The top 25 by each of total time, mean time, blocks read and temp blocks written, deduplicated. Severity follows the share of all statement time: high from 25%, medium from 10%, else low. The Finding has no collection, so the lifecycle neither asks "fixed?" nor marks it obsolete (ADR 0008): a statement missing from a later top 25 isn't fixed.

**Partial means no ranking was made.** The Run's status is `partial` when the workload analyzer was asked for but didn't rank: the statistics were refused, or there is no source. `scope` has no `workload` key for a refusal. A ranked workload records an empty list of collections, because statements name none. `WorkloadReport` is stored with the Run as a snapshot (kind `workload`) and is in the Markdown and JSON reports.

**No source: a schema-only review.** The Run says how to enable `pg_stat_statements` (only the steps still missing: preload and restart, `CREATE EXTENSION`, `pg_read_all_stats`) and reviews the catalog: foreign keys that no valid, non-partial btree index starts with (`missing_index:<table>(<columns>)`, with `CREATE INDEX CONCURRENTLY` DDL), and tables read mostly by sequential scans (`missing_index:<table>:seq_scan_heavy`: at least 50 sequential scans, 10,000 rows, more than the index scans). Its scope is every table reviewed. Neither `slow_query` nor `missing_index` is in the lifecycle's relation categories yet (ADR 0008), so these Findings never ask "fixed?" and never become obsolete: a created index or dropped table leaves them open until acknowledged. That is deliberate until index advice (#17) says what its scope covers. The review is a fallback: with a source, index advice (#17) will propose indexes from the statements instead. Azure Query Store (#16) will become a second source.

## Considered Options

- **A `Connection` setting for the window:** deferred; it needs a migration for one number and the Run parameter covers the ticket.
- **Rank in SQL with `queryid`:** rejected: `queryid` changes across versions and object ids, which breaks Finding identity.
- **A new Finding category for sequential-scan-heavy tables:** rejected. The proposal's category list stays; the rule name distinguishes it from a proposed index.
