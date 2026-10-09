-- name: storage_stats
-- columns: schema, name, kind, heap_bytes, index_bytes, toast_bytes, total_bytes, estimated_rows, partitions, live_rows, dead_rows, modified_since_analyze, last_vacuum, last_analyze, autovacuum_disabled
-- min_version: 15
-- privilege: none (catalog only; size functions need no table privilege)
--
-- Every table, partitioned table and materialized view outside system schemas. A partitioned
-- table is summed over its leaf partitions, which are not listed on their own.
-- heap + index + toast = total: heap is the table without TOAST (main fork, FSM, VM) and toast
-- is the TOAST table plus its index. estimated_rows is pg_class.reltuples, NULL until analyzed
-- (for a partitioned table: until every leaf partition is).
-- Activity comes from pg_stat_user_tables: live/dead/modified counters are summed over leaves,
-- last_vacuum and last_analyze (manual or auto, the later) are the oldest leaf's, NULL if any
-- leaf never had one. autovacuum_disabled: the reloption is off on the table or any leaf.
WITH rels AS (
  SELECT c.oid, n.nspname AS schema, c.relname AS name, c.relkind
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'p', 'm') AND NOT c.relispartition
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
),
parts AS (
  SELECT r.oid AS rel, coalesce(leaf.relid, r.oid) AS part
  FROM rels r
  LEFT JOIN LATERAL (
    SELECT t.relid FROM pg_partition_tree(r.oid) t WHERE r.relkind = 'p' AND t.isleaf
  ) leaf ON true
),
sized AS (
  SELECT p.rel, p.part,
         pg_total_relation_size(p.part) AS total,
         pg_indexes_size(p.part) AS index,
         coalesce(pg_total_relation_size(nullif(c.reltoastrelid, 0)), 0) AS toast,
         nullif(c.reltuples, -1) AS reltuples,
         st.n_live_tup, st.n_dead_tup, st.n_mod_since_analyze,
         greatest(st.last_vacuum, st.last_autovacuum) AS vacuumed,
         greatest(st.last_analyze, st.last_autoanalyze) AS analyzed,
         EXISTS (
           SELECT 1 FROM pg_options_to_table(c.reloptions) o
           WHERE o.option_name = 'autovacuum_enabled'
             AND lower(o.option_value) IN ('false', 'off', 'no', 'f', 'n', '0')
         ) AS autovacuum_off
  FROM parts p JOIN pg_class c ON c.oid = p.part
  LEFT JOIN pg_stat_user_tables st ON st.relid = p.part
)
SELECT r.schema, r.name,
       CASE r.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned_table'
                      ELSE 'matview' END AS kind,
       sum(s.total - s.index - s.toast)::bigint AS heap_bytes,
       sum(s.index)::bigint AS index_bytes,
       sum(s.toast)::bigint AS toast_bytes,
       sum(s.total)::bigint AS total_bytes,
       CASE WHEN bool_and(s.reltuples IS NOT NULL) THEN sum(s.reltuples)::bigint END
         AS estimated_rows,
       CASE WHEN r.relkind = 'p' THEN count(*) FILTER (WHERE s.part <> r.oid) END AS partitions,
       coalesce(sum(s.n_live_tup), 0)::bigint AS live_rows,
       coalesce(sum(s.n_dead_tup), 0)::bigint AS dead_rows,
       coalesce(sum(s.n_mod_since_analyze), 0)::bigint AS modified_since_analyze,
       CASE WHEN bool_and(s.vacuumed IS NOT NULL) THEN min(s.vacuumed) END AS last_vacuum,
       CASE WHEN bool_and(s.analyzed IS NOT NULL) THEN min(s.analyzed) END AS last_analyze,
       bool_or(s.autovacuum_off) AS autovacuum_disabled
FROM rels r JOIN sized s ON s.rel = r.oid
GROUP BY r.oid, r.schema, r.name, r.relkind
ORDER BY total_bytes DESC, r.schema, r.name
