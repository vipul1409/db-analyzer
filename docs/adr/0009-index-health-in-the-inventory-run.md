# Index health in the inventory Run, and Findings that belong to a collection

**Status:** accepted (#13)

**Index health runs as part of the inventory Run.** Unused, duplicate or overlapping, and invalid indexes all come from the catalog (`queries/index_stats.sql`), exactly like sizes. So every inventory Run checks the indexes of the collections it measured, and a targeted Run checks only its tables' indexes. The `index_advice` analyzer (#18) will add candidate indexes and HypoPG validation later. Health stays with inventory, so a plain `dbx analyze` reports it.

**One Finding per index, at most.** Each index is checked in this order:

- **Invalid:** reported first and left out of every other check. A failed concurrent build is the usual cause.
- **Duplicate:** the same table, access method, key columns (with operator class, collation and ordering), INCLUDE columns and predicate as another index. One member of the group is kept: preferably one that backs a constraint, then a unique one, then the first by name. Every other member is reported. Scan counts aren't used to choose, because they change between Runs and would move the Finding from one index to the other.
- **Overlapping:** a non-unique btree whose keys are a strict prefix of another btree's on the same table, with the same predicate. A unique prefix enforces something the wider index doesn't. Both duplicate and overlapping are `duplicate_index` Findings fingerprinted by index name, as the proposal's fingerprint table says, and the evidence `kind` tells them apart.
- **Unused:** zero scans since the statistics reset, and not a primary key, a unique index or a constraint-backing index. Every member of a duplicate group is excluded too, because the planner may use either one (ground truth), and so is any index already reported as overlapping. The Finding shows the reset time and its age in days, and says when that window is under 30 days or was counted on a replica.

Evidence names key columns but never shows expression or predicate text, which can hold literal values. `keys` (the precise comparison form) stays inside the analyzer.

**Findings carry the collection they belong to.** ADR 0008 decided coverage by checking the Finding's subject against the Run's scope, which only works when the subject is a collection. An index Finding's subject is the index, so `Finding.collection` now records the collection a subject is or belongs to. Migration 0007 backfills it for the existing collection categories. A Run covers a Finding when its analyzer measured that collection. Run comparison uses the same rule. A Finding becomes obsolete when its subject relation is gone, so dropping an index obsoletes its Finding while its table remains. Postgres relation names share one namespace per schema, so a single set of qualified collection and index names answers "does it still exist" for both. Hotspot Findings, whose subject includes a collection, will use the same field.

**The template uses no set-returning function in FROM other than catalog row sources.** `generate_series` would make the statement gated (it isn't a catalog row source), and the gate would then refuse catalog reads under tight limits. Instead, the index's own `pg_attribute` rows number its columns. Like `storage_stats`, it uses only `pg_partition_tree`, an allowlisted catalog row source, so the query stays catalog-only and exempt.

**Ready-to-copy DDL is what Postgres accepts.** `DROP INDEX CONCURRENTLY` is used for a plain index, and `DROP INDEX` for a partitioned one, which can't be dropped concurrently. A duplicate that backs a constraint gets no DDL; its recommendation says to drop the constraint instead. The primary key is the member kept first.

**Partitioned indexes are judged as a whole.** A partitioned index is invalid while any partition lacks an attached index, so its Finding points at `ATTACH PARTITION` rather than at a failed build. Indexes on individual partitions aren't listed. An invalid index left by a failed concurrent build on one partition, or an index created directly on a partition, isn't seen. That is a known gap.

## Considered Options

- **A separate `index_advice` Run for health:** rejected for now. It would need its own scope and a second Run per `dbx analyze` before the advisor exists.
- **Subject `table:index` so coverage can parse the table out:** rejected. It breaks the proposal's fingerprint rule (index name), and parsing fingerprints is fragile when names need quoting.
- **Add `generate_series` to the gate's catalog row sources:** rejected. It reads no relation, but it can produce any number of rows, and an exemption means no cost check.
