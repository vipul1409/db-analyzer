-- name: scan_activity
-- columns: schema, name, kind, seq_scans, seq_rows_read, idx_scans, live_rows
-- min_version: 15
-- privilege: none (catalog only)
--
-- How each table, partitioned table and materialized view outside system schemas has been read
-- since the statistics were last reset. A partitioned table is summed over its leaf
-- partitions, which are not listed on their own. seq_rows_read is seq_tup_read: rows a
-- sequential scan fetched. idx_scans counts index scans on the table's heap, not on its
-- indexes' own pages.
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
)
SELECT r.schema, r.name,
       CASE r.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned_table'
                      ELSE 'matview' END AS kind,
       coalesce(sum(st.seq_scan), 0)::bigint AS seq_scans,
       coalesce(sum(st.seq_tup_read), 0)::bigint AS seq_rows_read,
       coalesce(sum(st.idx_scan), 0)::bigint AS idx_scans,
       coalesce(sum(st.n_live_tup), 0)::bigint AS live_rows
FROM rels r
JOIN parts p ON p.rel = r.oid
LEFT JOIN pg_stat_user_tables st ON st.relid = p.part
GROUP BY r.oid, r.schema, r.name, r.relkind
ORDER BY r.schema, r.name
