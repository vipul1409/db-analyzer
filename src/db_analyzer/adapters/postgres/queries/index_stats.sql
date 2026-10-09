-- name: index_stats
-- columns: schema, name, table_schema, table_name, table_kind, method, keys, columns, include, predicate, index_bytes, scans, is_unique, is_primary, backs_constraint, is_valid, is_partitioned, nulls_not_distinct
-- min_version: 15
-- privilege: none (catalog only; size functions need no table privilege)
--
-- Catalog only, with no set-returning functions in FROM, so the EXPLAIN gate exempts it: an
-- index's own pg_attribute rows (attnum 1..indnatts) number its columns.
-- Every index on a collection storage_stats lists (tables, partitioned tables, materialized
-- views outside system schemas, partitions excluded). A partitioned index sums the sizes and
-- scans of its leaf indexes, which are not listed on their own.
-- keys: one entry per key column, precise enough to compare indexes (column or expression,
-- operator class, collation, ordering options). columns: the key column names, NULL for an
-- expression. include: INCLUDE columns. predicate: a partial index's WHERE clause.
-- scans: idx_scan since the statistics were last reset.
-- backs_constraint: the index enforces a primary key, unique or exclusion constraint.
-- is_valid: for a partitioned index, false also while a partition has no index attached.
-- Indexes on partitions are not listed: an invalid one left by a failed build on a single
-- partition is not seen.
WITH idx AS (
  SELECT i.indexrelid, i.indrelid, i.indnkeyatts, i.indnatts, i.indkey, i.indclass,
         i.indcollation, i.indoption, i.indpred, i.indisunique, i.indisprimary, i.indisvalid,
         i.indnullsnotdistinct,
         ic.relname AS name, ic.relkind, n.nspname AS schema, tn.nspname AS table_schema,
         t.relname AS table_name, t.relkind AS table_relkind, am.amname AS method
  FROM pg_index i
  JOIN pg_class ic ON ic.oid = i.indexrelid
  JOIN pg_namespace n ON n.oid = ic.relnamespace
  JOIN pg_class t ON t.oid = i.indrelid
  JOIN pg_namespace tn ON tn.oid = t.relnamespace
  JOIN pg_am am ON am.oid = ic.relam
  WHERE t.relkind IN ('r', 'p', 'm') AND NOT t.relispartition
    AND tn.nspname NOT IN ('pg_catalog', 'information_schema')
    AND tn.nspname NOT LIKE 'pg_toast%' AND tn.nspname NOT LIKE 'pg_temp%'
),
parts AS (
  SELECT x.indexrelid, coalesce(leaf.relid, x.indexrelid) AS part
  FROM idx x
  LEFT JOIN LATERAL (
    SELECT p.relid FROM pg_partition_tree(x.indexrelid) p WHERE x.relkind = 'I' AND p.isleaf
  ) leaf ON true
),
usage AS (
  SELECT p.indexrelid, sum(pg_relation_size(p.part))::bigint AS index_bytes,
         coalesce(sum(s.idx_scan), 0)::bigint AS scans
  FROM parts p LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = p.part
  GROUP BY p.indexrelid
)
SELECT x.schema, x.name, x.table_schema, x.table_name,
       CASE x.table_relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned_table'
                            ELSE 'matview' END AS table_kind,
       x.method,
       ARRAY(
         SELECT pg_get_indexdef(x.indexrelid, k.attnum, true)
                || ' ' || coalesce(oc.opcname, '') || ' ' || coalesce(co.collname, '')
                || ' ' || x.indoption[k.attnum - 1]
         FROM pg_attribute k
         LEFT JOIN pg_opclass oc ON oc.oid = x.indclass[k.attnum - 1]
         LEFT JOIN pg_collation co ON co.oid = x.indcollation[k.attnum - 1]
         WHERE k.attrelid = x.indexrelid AND k.attnum <= x.indnkeyatts
         ORDER BY k.attnum
       ) AS keys,
       ARRAY(
         SELECT a.attname::text
         FROM pg_attribute k
         LEFT JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = x.indkey[k.attnum - 1]
                                 AND x.indkey[k.attnum - 1] <> 0
         WHERE k.attrelid = x.indexrelid AND k.attnum <= x.indnkeyatts
         ORDER BY k.attnum
       ) AS columns,
       ARRAY(
         SELECT pg_get_indexdef(x.indexrelid, k.attnum, true)
         FROM pg_attribute k
         WHERE k.attrelid = x.indexrelid AND k.attnum > x.indnkeyatts
         ORDER BY k.attnum
       ) AS include,
       pg_get_expr(x.indpred, x.indrelid, true) AS predicate,
       u.index_bytes, u.scans,
       x.indisunique AS is_unique, x.indisprimary AS is_primary,
       EXISTS (
         SELECT 1 FROM pg_constraint c
         WHERE c.conindid = x.indexrelid AND c.contype IN ('p', 'u', 'x')
       ) AS backs_constraint,
       x.indisvalid AS is_valid,
       x.relkind = 'I' AS is_partitioned,
       x.indnullsnotdistinct AS nulls_not_distinct
FROM idx x JOIN usage u ON u.indexrelid = x.indexrelid
ORDER BY x.schema, x.name
