"""The privacy filter's output check for SQL the LLM wrote (proposal §3.5): every output
expression must be an aggregate, metadata from the catalog, or a confirmed entity key."""

import pytest

from db_analyzer.core.model import EntityKey, PrivacyRejected
from db_analyzer.safety import privacy

TENANT = EntityKey("public", "events", "tenant_id")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM accounts",
        "SELECT email, count(*) FROM accounts GROUP BY email",
    ],
)
def test_row_data_in_the_output_is_rejected(sql: str) -> None:
    with pytest.raises(PrivacyRejected, match="email"):
        privacy.check_output(sql, frozenset())


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM accounts",
        "SELECT count(*) FROM accounts WHERE email LIKE '%@example.test'",
    ],
)
def test_aggregates_pass(sql: str) -> None:
    privacy.check_output(sql, frozenset())


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1, 'x', now(), current_user, $1",
        "SELECT count(DISTINCT email), count(notes), regr_count(amount, id) FROM bookings",
        "SELECT avg(length(notes)), sum(pg_column_size(payload) + octet_length(kind)) FROM events",
        "SELECT sum(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) FROM bookings",
        "SELECT round(100.0 * count(*) / sum(count(*)) OVER (), 1) FROM events GROUP BY kind",
        "SELECT max(length(notes)), min(octet_length(payload::text)::bigint) FROM bookings, events",
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY pg_column_size(e.*)) FROM events e",
        "SELECT count(*) FILTER (WHERE status = 'pending') FROM bookings",
        "SELECT EXISTS (SELECT 1 FROM accounts WHERE email = 'a@b.c')",
        "SELECT (SELECT count(*) FROM accounts) AS n",
        "SELECT rank() OVER (ORDER BY count(*) DESC) FROM events GROUP BY tenant_id",
        "WITH c AS (SELECT kind, count(*) AS n FROM events GROUP BY kind) SELECT n FROM c",
        "WITH c AS (SELECT count(*) AS n FROM events) SELECT * FROM c",
        "SELECT s.n FROM (SELECT count(*) FROM events) AS s(n)",
        "SELECT count(*) FROM events UNION ALL SELECT count(*) FROM accounts",
        "VALUES (1), (2)",
        "SELECT relname, reltuples, pg_total_relation_size(oid) FROM pg_class",
        "SELECT c.relname, n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace",
        "SELECT * FROM pg_stat_user_tables",
        "SELECT queryid, calls, total_exec_time FROM public.pg_stat_statements",
        "SELECT t.relid FROM pg_class r, LATERAL pg_partition_tree(r.oid) t",
        "SELECT g FROM generate_series(1, 3) g",
        "SELECT schemaname, tablename, attname, n_distinct, null_frac FROM pg_stats",
        "SELECT schemaname, tablename, attname, most_common_vals, most_common_freqs "
        "FROM pg_stats WHERE tablename = 'events'",
        "SELECT * FROM information_schema.columns WHERE table_name = 'events'",
        "EXPLAIN SELECT email FROM accounts",
        "SHOW work_mem",
    ],
)
def test_aggregates_metadata_and_constants_pass(sql: str) -> None:
    privacy.check_output(sql, frozenset())


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT tenant_id, count(*) FROM events GROUP BY tenant_id",
        "SELECT e.tenant_id FROM public.events e",
        "SELECT public.events.tenant_id FROM public.events",
        "SELECT tenant_id FROM events JOIN tenants t ON t.id = events.tenant_id",
        "SELECT max(tenant_id) FROM events",
        "WITH c AS (SELECT tenant_id, count(*) AS n FROM events GROUP BY 1) SELECT * FROM c",
    ],
)
def test_confirmed_entity_keys_pass(sql: str) -> None:
    privacy.check_output(sql, frozenset({TENANT}))


def test_an_entity_key_of_one_table_does_not_cover_another() -> None:
    with pytest.raises(PrivacyRejected, match="tenant_id"):
        privacy.check_output("SELECT tenant_id FROM audit_log", frozenset({TENANT}))


def test_sampled_values_are_withheld_except_for_entity_keys() -> None:
    sql = "SELECT schemaname, tablename, attname AS col, most_common_vals FROM pg_stats"
    rows = [
        {
            "schemaname": "public",
            "tablename": "events",
            "col": "tenant_id",
            "most_common_vals": "{7}",
        },
        {
            "schemaname": "public",
            "tablename": "accounts",
            "col": "email",
            "most_common_vals": "{a}",
        },
    ]

    shown = privacy.check_output(sql, frozenset({TENANT})).apply(rows)

    assert [r["most_common_vals"] for r in shown] == ["{7}", privacy.WITHHELD]


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ('{"email": "user5@example.test"}', ["email address"]),
        ("call +44 20 7946 0958 today", ["phone number"]),
        (
            '{"total_bytes": 123456789012, "at": "2026-10-09T12:00:00+00:00", '
            '"sql": "SELECT count(*) FROM events WHERE kind = $1", "ratio": -0.25}',
            [],
        ),
    ],
)
def test_personal_data_bound_for_the_llm_is_found(text: str, found: list[str]) -> None:
    assert privacy.find_personal_data(text) == found
