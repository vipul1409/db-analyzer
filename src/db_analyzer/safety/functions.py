"""Which functions and relations QueryGuard accepts, and which ones the EXPLAIN gate exempts.

Built from pg_catalog.json, a snapshot of PG 15-18 (regenerate with
`uv run python -m tests.fixtures.catalog_snapshot`). A pg_catalog function is allowed when it is
stable or immutable, which by contract means it changes nothing, unless it is in DENIED. Volatile
functions are refused unless listed in ALLOWED_VOLATILE. Names are checked, not signatures: an
unqualified call is assumed to resolve to pg_catalog, which is first on every search path; a
same-named function in a user schema could only be planted by a role that can create functions
there, which the read-only self-check already rules out for the analyzer itself.
"""

import json
from pathlib import Path

_SNAPSHOT = json.loads((Path(__file__).parent / "pg_catalog.json").read_text())

# Stable or immutable, but they read whole tables (bypassing the gate and the privacy filter),
# run SQL text, or assign a transaction ID.
DENIED = frozenset(
    {
        "table_to_xml",
        "table_to_xml_and_xmlschema",
        "table_to_xmlschema",
        "schema_to_xml",
        "schema_to_xml_and_xmlschema",
        "schema_to_xmlschema",
        "database_to_xml",
        "database_to_xml_and_xmlschema",
        "database_to_xmlschema",
        "ts_rewrite",
        "txid_current",
        "txid_current_if_assigned",
        "pg_current_xact_id",
        "pg_current_xact_id_if_assigned",
    }
)

# Volatile only because their result can change between calls; they have no side effects.
ALLOWED_VOLATILE = frozenset(
    {
        "pg_relation_size",
        "pg_table_size",
        "pg_indexes_size",
        "pg_total_relation_size",
        "pg_database_size",
        "pg_tablespace_size",
        "pg_partition_tree",
        "pg_partition_ancestors",
        "pg_is_in_recovery",
        "pg_last_wal_receive_lsn",
        "pg_last_wal_replay_lsn",
        "pg_last_xact_replay_timestamp",
        "pg_current_wal_lsn",
        "pg_current_wal_insert_lsn",
        "pg_current_wal_flush_lsn",
        "pg_blocking_pids",
        "pg_lock_status",
        "pg_stat_have_stats",
        "pg_jit_available",
        "pg_collation_actual_version",
        "pg_database_collation_actual_version",
        "pg_xact_commit_timestamp",
        "clock_timestamp",
        "timeofday",
        "random",
        "random_normal",
        "gen_random_uuid",
        "uuidv4",
        "uuidv7",
    }
)

# Extension functions, allowed in whatever schema the extension lives in (proposal §3.3).
# HypoPG's indexes are backend-local and in memory: nothing is written to disk or catalogs.
EXTENSION_FUNCTIONS = frozenset(
    {
        "hypopg",
        "hypopg_create_index",
        "hypopg_drop_index",
        "hypopg_get_indexdef",
        "hypopg_relation_size",
        "hypopg_reset",
        "hypopg_reset_index",
        "pgstattuple_approx",
        "pg_stat_statements",
        "pg_stat_statements_info",
    }
)

PG_CATALOG_FUNCTIONS = (frozenset(_SNAPSHOT["stable_functions"]) - DENIED) | ALLOWED_VOLATILE

# --- EXPLAIN gate exemption -----------------------------------------------------------------

# pg_catalog relations (tables and views). Unqualified names resolve to pg_catalog first.
CATALOG_RELATIONS = frozenset(_SNAPSHOT["relations"])
CATALOG_SCHEMAS = frozenset({"pg_catalog", "information_schema"})
# Extension statistics views, in whatever schema the extension lives in.
EXTENSION_RELATIONS = frozenset(
    {"pg_stat_statements", "pg_stat_statements_info", "hypopg_list_indexes"}
)
# Row sources in FROM that read only catalog or in-memory state.
CATALOG_ROW_SOURCES = frozenset(
    {
        "pg_partition_tree",
        "pg_partition_ancestors",
        "pg_options_to_table",
        "pg_get_keywords",
        "pg_lock_status",
        "pg_show_all_settings",
        "pg_stat_statements",
        "pg_stat_statements_info",
        "hypopg",
        "hypopg_create_index",
    }
)
# Allowed, but they produce rows or read relation data, so a statement calling them is gated
# (unless it is a catalog row source).
SET_RETURNING = frozenset(_SNAPSHOT["set_returning_functions"])
READS_RELATION_DATA = frozenset({"pgstattuple_approx"})
