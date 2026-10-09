"""QueryGuard rules beyond the forbidden-statement corpus (tests/safety): what each profile
accepts, how statements are classified, and which ones the EXPLAIN gate exempts."""

import pytest

from db_analyzer.core.model import QueryRejected
from db_analyzer.safety import guard

BOTH = ("agent", "internal")


@pytest.mark.parametrize("profile", BOTH)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT count(*), sum(qty * price) FROM booking_items WHERE booking_id = 7",
        "WITH t AS (SELECT tenant_id, count(*) AS n FROM events GROUP BY 1) SELECT * FROM t",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5) "
        "SELECT * FROM r",
        "SELECT tenant_id, rank() OVER (ORDER BY count(*) DESC) FROM events GROUP BY 1",
        "SELECT e.id FROM events e, LATERAL jsonb_each(e.payload) j LIMIT 5",
        "SELECT pg_size_pretty(pg_total_relation_size('public.events'::regclass))",
        "SELECT pg_catalog.lower(relname) FROM pg_catalog.pg_class",
        "SELECT * FROM information_schema.columns WHERE table_name = 'events'",
        "SELECT query, calls FROM public.pg_stat_statements ORDER BY total_exec_time DESC",
        "SELECT * FROM pgstattuple_approx('public.audit_log')",
        "SELECT * FROM hypopg_create_index('CREATE INDEX ON bookings (account_id)')",
        "SELECT hypopg_reset()",
        "SELECT now(), clock_timestamp(), random(), extract(epoch FROM now())",
        "SELECT * FROM generate_series(1, 10)",
        "SELECT 1 UNION SELECT 2",
        "VALUES (1), (2)",
        "EXPLAIN SELECT * FROM events WHERE tenant_id = 7",
        "EXPLAIN (FORMAT JSON, COSTS ON, VERBOSE) SELECT 1",
        "EXPLAIN (GENERIC_PLAN) SELECT * FROM bookings WHERE account_id = $1",
        "SHOW work_mem",
        "SHOW ALL",
        "SELECT 1;",
        "  -- a comment\nSELECT 1 /* another */",
    ],
)
def test_read_only_statements_pass_both_profiles(sql: str, profile: guard.Profile) -> None:
    guard.check(sql, profile)


@pytest.mark.parametrize(
    "sql",
    [
        "PREPARE p(int) AS SELECT * FROM bookings WHERE account_id = $1",
        "EXPLAIN (FORMAT JSON) EXECUTE p(NULL)",
        "EXPLAIN EXECUTE p",
        "DEALLOCATE p",
        "DEALLOCATE ALL",
        "SET LOCAL plan_cache_mode = force_generic_plan",
        "SET LOCAL plan_cache_mode TO auto",
    ],
)
def test_generic_plan_statements_are_internal_only(sql: str) -> None:
    guard.check(sql, "internal")
    with pytest.raises(QueryRejected, match="agent"):
        guard.check(sql, "agent")


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT 1", "select"),
        ("EXPLAIN SELECT 1", "explain"),
        ("EXPLAIN EXECUTE p", "explain"),
        ("SHOW work_mem", "show"),
        ("SET LOCAL plan_cache_mode = force_generic_plan", "set_local"),
        ("PREPARE p AS SELECT 1", "prepare"),
        ("DEALLOCATE p", "deallocate"),
    ],
)
def test_statement_kind(sql: str, kind: str) -> None:
    assert guard.check(sql, "internal").kind == kind


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN SELECT * FROM events",
        "SHOW work_mem",
        "PREPARE p AS SELECT * FROM events",
        "DEALLOCATE p",
        "SET LOCAL plan_cache_mode = force_generic_plan",
        "SELECT relname, reltuples FROM pg_class WHERE relkind = 'r'",
        "SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM pg_stat_user_tables",
        "SELECT stats_reset FROM public.pg_stat_statements_info",
        "WITH c AS (SELECT oid FROM pg_class) SELECT count(*) FROM c",
        "SELECT pg_total_relation_size('public.events')",
        "SELECT t.relid FROM pg_class c, LATERAL pg_partition_tree(c.oid) t",
        "SELECT * FROM hypopg_create_index('CREATE INDEX ON events (kind)')",
        "SELECT current_setting('work_mem'), version()",
    ],
)
def test_catalog_only_and_non_executing_statements_are_exempt_from_the_gate(sql: str) -> None:
    assert guard.check(sql, "internal").gated is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM events",
        "SELECT * FROM public.events",
        "SELECT c.relname, e.id FROM pg_class c JOIN events e ON e.id = c.oid::int",
        "SELECT * FROM pg_stat_user_tables s WHERE s.relid IN (SELECT tenant_id FROM accounts)",
        "WITH c AS (SELECT * FROM events) SELECT count(*) FROM c",
        "SELECT * FROM generate_series(1, 1000000000)",
        "SELECT * FROM pgstattuple_approx('public.audit_log')",
        "SELECT * FROM pg_myview",  # not a pg_catalog relation, so a user relation
        "SELECT 1 FROM pg_class UNION ALL SELECT 1 FROM events",
    ],
)
def test_statements_reading_user_data_are_gated(sql: str) -> None:
    assert guard.check(sql, "internal").gated is True


@pytest.mark.parametrize("profile", BOTH)
@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT pg_sleep(1)", "function pg_sleep is not allowed"),
        ("SELECT pg_catalog.pg_sleep(1)", "function pg_catalog.pg_sleep is not allowed"),
        ("SELECT public.lower('a')", "function public.lower is not allowed"),
        ("SELECT my_func()", "function my_func is not allowed"),
        ("SELECT 1 WHERE 1 OPERATOR(public.===) 1", "operator public.=== is not allowed"),
        ("EXPLAIN ANALYZE SELECT 1", "EXPLAIN ANALYZE"),
        ("EXPLAIN (ANALYZE false) SELECT 1", "EXPLAIN ANALYZE"),
        ("EXPLAIN DELETE FROM events", "EXPLAIN may only wrap"),
        ("PREPARE p AS DELETE FROM events", "PREPARE may only wrap"),
        ("SET LOCAL work_mem = '1GB'", "SET"),
        ("SELECT 1; SELECT 2", "exactly one statement"),
    ],
)
def test_rejections_name_the_reason(sql: str, reason: str, profile: guard.Profile) -> None:
    with pytest.raises(QueryRejected, match=reason.replace("(", r"\(")):
        guard.check(sql, profile)


@pytest.mark.parametrize(
    "sql",
    ["SELECT generate_series(1, 1000)", "SELECT unnest(ARRAY[1, 2]), pg_total_relation_size(1)"],
)
def test_set_returning_functions_in_the_select_list_are_gated(sql: str) -> None:
    assert guard.check(sql, "internal").gated is True


@pytest.mark.parametrize(
    "sql",
    [
        # A CTE name only shadows a table inside the WITH's own query.
        "SELECT * FROM users, (WITH users AS (SELECT 1) SELECT * FROM users) s",
        "SELECT (SELECT count(*) FROM (WITH audit_log AS (SELECT 1) SELECT 1) q), * FROM audit_log",
        # A non-recursive CTE sees only the CTEs before it.
        "WITH a AS (SELECT * FROM users), users AS (SELECT 1) SELECT * FROM a",
        # Recursion produces rows without reading any relation.
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r",
    ],
)
def test_cte_scope_cannot_hide_a_user_table_from_the_gate(sql: str) -> None:
    assert guard.check(sql, "agent").gated is True


@pytest.mark.parametrize(
    "sql",
    [
        "WITH users AS (SELECT oid FROM pg_class) SELECT * FROM users",
        "WITH a AS (SELECT oid FROM pg_class), b AS (SELECT * FROM a) SELECT * FROM b",
        "SELECT * FROM (WITH c AS (SELECT 1) SELECT * FROM c) s, pg_namespace",
    ],
)
def test_cte_over_the_catalog_stays_exempt(sql: str) -> None:
    assert guard.check(sql, "agent").gated is False
