-- name: identifiers
-- columns: kind, name
-- min_version: 15
-- privilege: none (catalog only)
--
-- Every name in user schemas the LLM must see only as an alias (Connection.alias_identifiers):
-- schemas, relations (tables, views, indexes, sequences, ...), columns and constraints.
WITH schemas AS (
  SELECT oid, nspname FROM pg_namespace
  WHERE nspname NOT IN ('pg_catalog', 'information_schema')
    AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp%'
),
rels AS (
  SELECT c.oid, c.relname, c.relkind FROM pg_class c JOIN schemas s ON s.oid = c.relnamespace
)
SELECT kind, name FROM (
  SELECT 1 AS ord, 'schema' AS kind, nspname AS name FROM schemas
  UNION ALL
  SELECT 2, CASE WHEN relkind IN ('i', 'I') THEN 'index'
                 WHEN relkind = 'S' THEN 'sequence' ELSE 'table' END, relname
  FROM rels
  UNION ALL
  SELECT 3, 'column', a.attname FROM pg_attribute a JOIN rels r ON r.oid = a.attrelid
  WHERE a.attnum > 0 AND NOT a.attisdropped
  UNION ALL
  SELECT 4, 'constraint', con.conname FROM pg_constraint con JOIN schemas s ON s.oid = con.connamespace
) names
GROUP BY kind, name
ORDER BY min(ord), name
