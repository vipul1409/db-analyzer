from dataclasses import replace
from datetime import UTC, datetime, timedelta

from db_analyzer.analyzers import index_health
from db_analyzer.core.model import CollectionKind, CollectionRef, IndexStats, Observed

MB = 1024 * 1024
NOW = datetime(2026, 10, 9, tzinfo=UTC)
ACCOUNTS = CollectionRef("public", "accounts", CollectionKind.TABLE)


def index(name: str, *keys: str, scans: int = 5, **kw: object) -> IndexStats:
    base = IndexStats(
        name=f"public.{name}",
        table=ACCOUNTS,
        method="btree",
        keys=list(keys),
        columns=list(keys),
        include=[],
        predicate=None,
        index_bytes=2 * MB,
        scans=scans,
        unique=False,
        primary=False,
        constraint=False,
        valid=True,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def analyze(
    *indexes: IndexStats, reset: datetime | None = NOW - timedelta(days=40)
) -> dict[str, Observed]:
    found = index_health.analyze(list(indexes), stats_reset=reset, now=NOW, on_replica=False)
    return {o.fingerprint: o for o in found}


def test_an_index_never_scanned_is_unused_with_the_stats_window_as_context() -> None:
    found = analyze(index("idx_created", "created_at", scans=0))

    unused = found["unused_index:public.idx_created"]
    assert unused.collection == "public.accounts"
    assert unused.evidence["scans"] == 0
    assert unused.evidence["stats_reset"] == (NOW - timedelta(days=40)).isoformat()
    assert unused.evidence["stats_age_days"] == 40
    assert "40 days" in unused.title
    assert unused.ddl == "DROP INDEX CONCURRENTLY public.idx_created;"


def test_a_recent_stats_reset_is_called_out() -> None:
    found = analyze(index("idx_created", "created_at", scans=0), reset=NOW - timedelta(days=2))

    assert "only 2 days" in (found["unused_index:public.idx_created"].recommendation or "")


def test_stats_never_reset_counts_since_statistics_began() -> None:
    found = analyze(index("idx_created", "created_at", scans=0), reset=None)

    unused = found["unused_index:public.idx_created"]
    assert unused.evidence["stats_reset"] is None
    assert "since statistics began" in unused.title


def test_primary_unique_and_constraint_indexes_are_never_unused() -> None:
    found = analyze(
        index("accounts_pkey", "id", scans=0, primary=True, unique=True, constraint=True),
        index("accounts_email_key", "email", scans=0, unique=True),
        index("accounts_excl", "tenant_id", scans=0, constraint=True),
    )

    assert not [f for f in found if f.startswith("unused_index:")]


def test_scanned_indexes_are_not_unused() -> None:
    assert analyze(index("idx_created", "created_at", scans=1)) == {}


def test_an_exact_duplicate_is_reported_once_and_neither_member_is_unused() -> None:
    found = analyze(
        index("idx_tenant", "tenant_id", scans=0), index("idx_tenant_dup", "tenant_id", scans=0)
    )

    assert set(found) == {"duplicate_index:public.idx_tenant_dup"}
    dup = found["duplicate_index:public.idx_tenant_dup"]
    assert dup.evidence["duplicate_of"] == "public.idx_tenant"
    assert dup.evidence["kind"] == "duplicate"
    assert dup.ddl == "DROP INDEX CONCURRENTLY public.idx_tenant_dup;"


def test_the_duplicate_kept_is_the_one_backing_a_constraint() -> None:
    found = analyze(
        index("a_plain", "email"), index("z_key", "email", unique=True, constraint=True)
    )

    assert found["duplicate_index:public.a_plain"].evidence["duplicate_of"] == "public.z_key"


def test_indexes_differing_in_method_predicate_or_include_are_not_duplicates() -> None:
    found = analyze(
        index("idx_a", "tenant_id"),
        index("idx_hash", "tenant_id", method="hash"),
        index("idx_partial", "tenant_id", predicate="(status = 'x')"),
    )

    assert found == {}


def test_a_btree_prefix_of_another_is_overlapping() -> None:
    found = analyze(index("idx_tenant", "tenant_id"), index("idx_tenant_at", "tenant_id", "at"))

    assert set(found) == {"duplicate_index:public.idx_tenant"}
    overlap = found["duplicate_index:public.idx_tenant"]
    assert overlap.evidence["kind"] == "overlapping"
    assert overlap.evidence["duplicate_of"] == "public.idx_tenant_at"
    assert overlap.severity == "low"


def test_a_unique_prefix_is_not_overlapping_because_it_enforces_uniqueness() -> None:
    found = analyze(
        index("idx_email", "email", unique=True), index("idx_email_at", "email", "created_at")
    )

    assert found == {}


def test_an_invalid_index_is_reported_and_left_out_of_the_other_checks() -> None:
    found = analyze(
        index("idx_status_unique", "status", scans=0, valid=False, unique=True),
        index("idx_status", "status"),
    )

    assert set(found) == {"invalid_index:public.idx_status_unique"}
    invalid = found["invalid_index:public.idx_status_unique"]
    assert invalid.severity == "medium"
    assert invalid.ddl == "DROP INDEX CONCURRENTLY public.idx_status_unique;"


def test_expression_keys_are_not_shown_in_evidence() -> None:
    expr = index("idx_lower", "lower(email)", scans=0, columns=[None])

    unused = analyze(expr)["unused_index:public.idx_lower"]

    assert unused.evidence["columns"] == ["(expression)"]
    assert "lower" not in str(unused.evidence)


def test_a_partitioned_index_is_dropped_without_concurrently() -> None:
    found = analyze(index("idx_usage_metric", "metric", scans=0, partitioned=True))

    assert (
        found["unused_index:public.idx_usage_metric"].ddl == "DROP INDEX public.idx_usage_metric;"
    )


def test_an_invalid_partitioned_index_points_at_unattached_partitions() -> None:
    found = analyze(index("idx_usage_at", "at", valid=False, partitioned=True))

    advice = found["invalid_index:public.idx_usage_at"].recommendation or ""
    assert "ATTACH PARTITION" in advice and "failed" not in advice


def test_a_duplicate_backing_a_constraint_gets_no_drop_index() -> None:
    found = analyze(
        index("accounts_pkey", "id", primary=True, unique=True, constraint=True),
        index("accounts_id_key", "id", unique=True, constraint=True),
    )

    dup = found["duplicate_index:public.accounts_id_key"]
    assert dup.evidence["duplicate_of"] == "public.accounts_pkey", "the primary key is kept"
    assert dup.ddl is None
    assert "DROP CONSTRAINT" in (dup.recommendation or "")


def test_an_index_reported_for_dropping_is_never_the_wider_one() -> None:
    found = analyze(
        index("idx_a", "a"),
        index("idx_ab", "a", "b"),
        index("idx_ab_dup", "a", "b"),
        index("idx_abc", "a", "b", "c"),
    )

    assert found["duplicate_index:public.idx_a"].evidence["duplicate_of"] == "public.idx_abc"


def test_unique_nulls_not_distinct_is_not_a_duplicate_of_plain_unique() -> None:
    found = analyze(
        index("idx_email", "email", unique=True),
        index("idx_email_nnd", "email", unique=True, nulls_not_distinct=True),
    )

    assert found == {}
