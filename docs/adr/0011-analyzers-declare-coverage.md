# Analyzers declare what they cover, and the Finding records it

**Status:** accepted; amends the coverage rule of ADR 0008 and 0009

ADR 0008 decided coverage with a map from Finding category to the one analyzer that evaluates it (`lifecycle.RELATION_CATEGORIES`). That breaks once two analyzers produce one category. `missing_index` already comes from the workload analyzer's schema-only review (ADR 0010), and index advice (#17) will propose them too. The map would have to choose one analyzer's scope for both.

**Each analyzer declares the categories it covers.** An `Analyzer` in `runs/` lists in `covers` the categories it checks completely for every collection in its scope. Inventory covers `size`, `bloat`, `stale_stats` and the three index categories. Workload covers none yet, for the reasons in ADR 0010.

**The Finding records the analyzer that covers it.** When a Run records an Observation, the Finding's `covered_by` is set to the observing analyzer if that analyzer covers the category, and to None otherwise. The latest Run to observe the Finding decides. Coverage is unchanged otherwise: a Run covers a Finding when `covered_by` measured its collection. Migration 0008 backfills `inventory` for the categories it covered before.

**One definition of a fact.** A Finding whose Observations have severity `info` is a fact (today, a table among the largest), not a problem. Facts are never covered, so they never ask "fixed?", and Run comparison and the CLI and agent summaries leave them out. This replaces `lifecycle.is_ranking_fact`, which recognised `size` Findings by their fingerprint, while the summaries filtered on severity.

**Obsolete is still a category property.** A Finding becomes obsolete when its subject relation is gone. Whether a subject is a relation depends on the category, not on who observed it, so `lifecycle.RELATION_CATEGORIES` stays as a set for that check alone.

## Considered Options

- **Keep the map and allow several analyzers per category:** rejected. A Run would cover a `missing_index` Finding from the schema review because index advice measured its table, even though index advice never checked for that kind of Finding.
- **Work out coverage on read from the latest Observation's Run:** rejected. Observations do not record which analyzer made them, and it would put a lookup in every lifecycle and comparison call.
