-- name: relation_estimates
-- columns: schema, name, estimated_rows, live_rows, modified_since_analyze, analyzed
-- min_version: 15
-- privilege: none (catalog only)
--
-- What the planner knows about every table, partition and materialized view outside system
-- schemas, for judging the estimates in a plan. Partitions are listed on their own, since plans
-- scan them, not their parent. estimated_rows is reltuples (-1 when never analyzed or
-- vacuumed); live_rows the running count of live rows; analyzed whether it was ever analyzed,
-- manually or automatically.
SELECT n.nspname AS schema, c.relname AS name,
       c.reltuples::bigint AS estimated_rows,
       coalesce(st.n_live_tup, 0)::bigint AS live_rows,
       coalesce(st.n_mod_since_analyze, 0)::bigint AS modified_since_analyze,
       (st.last_analyze IS NOT NULL OR st.last_autoanalyze IS NOT NULL) AS analyzed
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables st ON st.relid = c.oid
WHERE c.relkind IN ('r', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
ORDER BY n.nspname, c.relname
