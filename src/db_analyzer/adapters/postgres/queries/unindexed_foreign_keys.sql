-- name: unindexed_foreign_keys
-- columns: schema, name, kind, constraint_name, columns
-- min_version: 15
-- privilege: none (catalog only)
--
-- Catalog only, with no set-returning function in FROM, so the EXPLAIN gate exempts it.
-- Every foreign key (on a table or partitioned table outside system schemas; the copies a
-- partitioned table passes down to its partitions are skipped) for which no valid, non-partial
-- btree index on the same table starts with all of its columns, in any order. Without one,
-- deleting or updating a referenced row scans the referencing table. columns: the key columns
-- in constraint order.
SELECT n.nspname AS schema, t.relname AS name,
       CASE t.relkind WHEN 'p' THEN 'partitioned_table' ELSE 'table' END AS kind,
       c.conname AS constraint_name,
       ARRAY(
         SELECT a.attname::text FROM pg_attribute a
         WHERE a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
         ORDER BY array_position(c.conkey, a.attnum)
       ) AS columns
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE c.contype = 'f' AND c.conparentid = 0
  AND t.relkind IN ('r', 'p') AND NOT t.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
  AND NOT EXISTS (
    SELECT 1
    FROM pg_index i
    JOIN pg_class ic ON ic.oid = i.indexrelid
    JOIN pg_am am ON am.oid = ic.relam
    WHERE i.indrelid = c.conrelid AND am.amname = 'btree'
      AND i.indisvalid AND i.indpred IS NULL
      AND i.indnkeyatts >= cardinality(c.conkey)
      AND c.conkey <@ (i.indkey::int2[])[0:cardinality(c.conkey) - 1]
  )
ORDER BY n.nspname, t.relname, c.conname
