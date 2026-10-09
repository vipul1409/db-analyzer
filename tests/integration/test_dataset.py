"""The seeded 'shop' database really has every property the ground-truth file promises.

Observed as the superuser straight from the catalog, independent of any analyzer code.
"""

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from tests.fixtures.dataset import GROUND_TRUTH

from .conftest import SUPPORTED, seeded_dsn

pytestmark = pytest.mark.integration

FINDINGS = {f["fingerprint"]: f for f in GROUND_TRUTH["findings"]}
HOT = GROUND_TRUTH["hot_tenant_id"]


@pytest.fixture(scope="module", params=SUPPORTED)
def shop(request: pytest.FixtureRequest) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(seeded_dsn(request.param, role="postgres"), autocommit=True) as conn:
        yield conn


def one(conn: psycopg.Connection[Any], sql: str, *params: object) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def findings(category: str) -> set[str]:
    return {fp.split(":", 1)[1] for fp in FINDINGS if fp.startswith(category + ":")}


def test_largest_table_is_as_expected(shop: psycopg.Connection[Any]) -> None:
    largest = one(
        shop,
        """SELECT n.nspname || '.' || c.relname FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relispartition
           ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 1""",
    )
    assert largest == GROUND_TRUTH["inventory"]["largest_table"]


def test_fk_columns_without_leading_index_match(shop: psycopg.Connection[Any]) -> None:
    rows = shop.execute(
        """SELECT format('%s.%s(%s)', n.nspname, c.relname, a.attname)
           FROM pg_constraint k
           JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
           JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1]
           WHERE k.contype = 'f' AND n.nspname = 'public'
             AND NOT EXISTS (SELECT 1 FROM pg_index i
                             WHERE i.indrelid = k.conrelid AND i.indkey[0] = k.conkey[1])"""
    ).fetchall()
    assert {r[0] for r in rows} == findings("missing_index")


def test_unused_indexes_are_exactly_the_expected_ones(shop: psycopg.Connection[Any]) -> None:
    """Unused = never scanned, excluding PK/unique, invalid, and both members of a duplicate
    pair (identical indexes: the planner may use either)."""
    rows = shop.execute(
        """SELECT s.schemaname || '.' || s.indexrelname FROM pg_stat_user_indexes s
           JOIN pg_index i ON i.indexrelid = s.indexrelid
           WHERE s.idx_scan = 0 AND NOT i.indisunique AND i.indisvalid"""
    ).fetchall()
    duplicate_pairs = {
        name
        for fp, f in FINDINGS.items()
        if fp.startswith("duplicate_index:")
        for name in (fp.split(":", 1)[1], f["duplicate_of"])
    }
    assert {r[0] for r in rows} - duplicate_pairs == findings("unused_index")


def test_statistics_are_fresh_except_on_the_stale_table(shop: psycopg.Connection[Any]) -> None:
    rows = shop.execute(
        """SELECT schemaname || '.' || relname FROM pg_stat_user_tables
           WHERE last_analyze IS NULL AND last_autoanalyze IS NULL
             AND relid NOT IN (SELECT inhrelid FROM pg_inherits)"""
    ).fetchall()
    assert {r[0] for r in rows} == set()


def test_index_health_cases_exist(shop: psycopg.Connection[Any]) -> None:
    [dup] = findings("duplicate_index")
    original = FINDINGS[f"duplicate_index:{dup}"]["duplicate_of"]
    same = one(
        shop,
        """SELECT a.indrelid = b.indrelid AND a.indkey::text = b.indkey::text
           FROM pg_index a, pg_index b WHERE a.indexrelid = %s::regclass
           AND b.indexrelid = %s::regclass""",
        dup,
        original,
    )
    assert same is True

    [invalid] = findings("invalid_index")
    assert (
        one(shop, "SELECT indisvalid FROM pg_index WHERE indexrelid = %s::regclass", invalid)
        is False
    )


def test_bloated_table_has_dead_tuples(shop: psycopg.Connection[Any]) -> None:
    [table] = findings("bloat")
    live, dead = shop.execute(
        "SELECT tuple_count, dead_tuple_count FROM pgstattuple(%s::regclass)", (table,)
    ).fetchone() or (0, 0)
    assert dead >= FINDINGS[f"bloat:{table}"]["min_dead_tuple_ratio"] * live > 0


def test_stale_stats_table_has_far_too_few_estimated_rows(shop: psycopg.Connection[Any]) -> None:
    [table] = findings("stale_stats")
    estimated = one(shop, "SELECT reltuples FROM pg_class WHERE oid = %s::regclass", table)
    actual = one(shop, f"SELECT count(*) FROM {table}")
    assert 0 < estimated <= FINDINGS[f"stale_stats:{table}"]["max_reltuples_ratio"] * actual


def test_hot_tenant_owns_expected_share_of_events(shop: psycopg.Connection[Any]) -> None:
    f = FINDINGS[f"hotspot:tenant:{HOT}:public.events"]
    rows_share, bytes_share = shop.execute(
        """SELECT avg((tenant_id = %(hot)s)::int),
                  sum(pg_column_size(e.*)) FILTER (WHERE tenant_id = %(hot)s)::numeric
                    / sum(pg_column_size(e.*))
           FROM events e""",
        {"hot": HOT},
    ).fetchone() or (0, 0)
    assert abs(float(rows_share) - f["rows_share"]) <= f["tolerance"]
    assert abs(float(bytes_share) - f["bytes_share"]) <= f["tolerance"]


def test_partitioned_table_has_one_partition_per_tenant(shop: psycopg.Connection[Any]) -> None:
    f = FINDINGS[f"hotspot:tenant:{HOT}:public.usage_records"]
    key = one(shop, "SELECT pg_get_partkeydef('public.usage_records'::regclass)")
    assert key == "LIST (tenant_id)"
    partitions = one(
        shop, "SELECT count(*) FROM pg_inherits WHERE inhparent = 'public.usage_records'::regclass"
    )
    assert partitions == one(shop, "SELECT count(*) FROM tenants")
    hot_share = one(shop, "SELECT avg((tenant_id = %s)::int) FROM usage_records", HOT)
    assert abs(float(hot_share) - f["rows_share"]) <= f["tolerance"]


@pytest.mark.parametrize("query", GROUND_TRUTH["slow_queries"], ids=lambda q: q["match"][:30])
def test_workload_replay_recorded_slow_query(
    shop: psycopg.Connection[Any], query: dict[str, Any]
) -> None:
    calls, temp_blocks = shop.execute(
        """SELECT sum(calls), sum(temp_blks_written) FROM pg_stat_statements
           WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
             AND query LIKE '%%' || %s || '%%'""",
        (query["match"],),
    ).fetchone() or (None, None)
    assert calls and calls > 0
    if query.get("temp_blocks"):
        assert temp_blocks is not None and temp_blocks > 0


def test_analyzer_role_can_read_every_table(shop: psycopg.Connection[Any]) -> None:
    unreadable = shop.execute(
        """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
             AND NOT has_table_privilege('db_analyzer', c.oid, 'SELECT')"""
    ).fetchall()
    assert unreadable == []


def test_index_heavy_table_has_more_index_than_heap(shop: psycopg.Connection[Any]) -> None:
    [table] = {s.split(":")[0] for s in findings("size") if s.endswith(":index_heavy")}
    heap, index = shop.execute(
        "SELECT pg_relation_size(%s::regclass), pg_indexes_size(%s::regclass)", (table, table)
    ).fetchone() or (0, 0)
    assert index > heap >= 1024 * 1024


def test_tables_per_schema_match(shop: psycopg.Connection[Any]) -> None:
    rows = shop.execute(
        """SELECT n.nspname, count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname IN ('public', 'reference') AND c.relkind IN ('r', 'p')
             AND NOT c.relispartition GROUP BY 1"""
    ).fetchall()
    assert dict(rows) == GROUND_TRUTH["inventory"]["schemas"]
