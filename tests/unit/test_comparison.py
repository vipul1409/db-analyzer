from collections.abc import Sequence
from datetime import UTC, datetime

from db_analyzer.core.comparison import SizeChange, compare
from db_analyzer.core.model import CollectionKind, CollectionRef, Finding, Run, StorageStats

AT = datetime(2026, 10, 9, tzinfo=UTC)
GB = 1024**3


def ref(name: str) -> CollectionRef:
    return CollectionRef("public", name.removeprefix("public."), CollectionKind.TABLE)


def run(run_id: str, scope: list[str], skipped: Sequence[str] = (), at: datetime = AT) -> Run:
    return Run(
        id=run_id,
        connection_id="c",
        thread_id=None,
        scope={"inventory": [ref(n) for n in scope]},
        skipped={"inventory": [(ref(n), "gate") for n in skipped]} if skipped else {},
        started_at=at,
        finished_at=at,
        status="partial" if skipped else "complete",
    )


def stats(name: str, total: int) -> StorageStats:
    return StorageStats(ref(name), None, "estimate", total, 0, 0, total)


def seen(fingerprint: str) -> Finding:
    category, subject = fingerprint.split(":")[:2]
    return Finding("c", fingerprint, category, subject, "open", "r1", "r1", collection=subject)  # type: ignore[arg-type]


def test_size_changes_cover_only_collections_both_runs_measured() -> None:
    before = run("r1", ["public.events", "public.audit_log"])
    after = run("r2", ["public.events", "public.tenants"])

    c = compare(
        before,
        after,
        [stats("events", 10 * GB), stats("audit_log", 5 * GB)],
        [stats("events", 22 * GB), stats("tenants", 1 * GB)],
        [],
        [],
    )

    assert c.shared == {"inventory": ["public.events"]}
    assert c.size_changes == [SizeChange("public.events", 10 * GB, 22 * GB, 12 * GB)]
    assert c.not_compared == {"inventory": ["public.audit_log", "public.tenants"]}


def test_a_collection_skipped_by_either_run_is_not_compared() -> None:
    before = run("r1", ["public.events", "public.tenants"])
    after = run("r2", ["public.tenants"], skipped=["public.events"])

    c = compare(
        before, after, [stats("events", GB), stats("tenants", GB)], [stats("tenants", GB)], [], []
    )

    assert c.shared == {"inventory": ["public.tenants"]}
    assert c.not_compared == {"inventory": ["public.events"]}
    assert c.appeared == c.disappeared == []


def test_findings_appear_and_disappear_only_over_shared_scope() -> None:
    before = run("r1", ["public.events", "public.audit_log"])
    after = run("r2", ["public.events", "public.tenants"])

    c = compare(
        before,
        after,
        [],
        [],
        [seen("bloat:public.audit_log"), seen("stale_stats:public.events")],
        [seen("bloat:public.events"), seen("bloat:public.tenants")],
    )

    assert c.appeared == ["bloat:public.events"]
    assert c.disappeared == ["stale_stats:public.events"]


def test_size_rankings_are_not_compared_as_findings() -> None:
    r = ["public.events", "public.tenants"]

    c = compare(run("r1", r), run("r2", r), [], [], [seen("size:public.events")], [])

    assert c.disappeared == []


def test_runs_are_compared_oldest_first_whatever_the_argument_order() -> None:
    older = run("r1", ["public.events"], at=AT)
    newer = run("r2", ["public.events"], at=AT.replace(day=10))

    c = compare(newer, older, [stats("events", 2 * GB)], [stats("events", GB)], [], [])

    assert (c.before, c.after) == ("r1", "r2")
    assert c.size_changes == [SizeChange("public.events", GB, 2 * GB, GB)]
