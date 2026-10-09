# Azure Database for PostgreSQL – Flexible Server

Run `postgres_role.sql` as the server admin user, as for self-managed Postgres. Azure-specific notes:

- `pg_monitor` can be granted by the admin user.
- Extensions must first be allow-listed in the `azure.extensions` server parameter, then created by an admin in the target database. DB Analyzer never creates extensions; it only detects them.
  - `pg_stat_statements` also needs `shared_preload_libraries` to include it (server restart).
  - `hypopg` and `pgstattuple` are optional; without them, index advice is marked unvalidated and bloat uses statistics only.
- Without `pg_stat_statements`, workload is read from Query Store in the `azure_sys` database. Grant the same role `CONNECT` on `azure_sys`.
