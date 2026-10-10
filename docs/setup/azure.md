# Azure Database for PostgreSQL – Flexible Server

Run `postgres_role.sql` as the server admin user, as for self-managed Postgres. Azure-specific notes:

- `pg_monitor` can be granted by the admin user.
- Extensions must first be allow-listed in the `azure.extensions` server parameter, then created by an admin in the target database. DB Analyzer never creates extensions; it only detects them.
  - `pg_stat_statements` also needs `shared_preload_libraries` to include it (server restart).
  - `hypopg` and `pgstattuple` are optional; without them, index advice is marked unvalidated and bloat uses statistics only.
- Without `pg_stat_statements`, workload is read from Query Store in the `azure_sys` database, through an auxiliary session with the same login. It needs:
  - the server parameter `pg_qs.query_capture_mode` set to `top` (or `all`); the first data appears after up to 20 minutes;
  - `CONNECT` on `azure_sys` for the analyzer role (`GRANT CONNECT ON DATABASE azure_sys TO db_analyzer;`). Query Store's views are readable by `PUBLIC`.
  - The login must pass the same read-only self-check in `azure_sys` as in the target database.
- Query Store support is tested against fixture tables that mimic `query_store.qs_view`; verification against a live Azure server is deferred until access exists.
