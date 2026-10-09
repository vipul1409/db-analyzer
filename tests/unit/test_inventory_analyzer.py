from db_analyzer.analyzers import inventory
from db_analyzer.core.model import CollectionKind, CollectionRef, StorageStats


def stats(name: str, total: int, rows: int | None = 10) -> StorageStats:
    return StorageStats(
        ref=CollectionRef(namespace="public", name=name, kind=CollectionKind.TABLE),
        row_count=rows,
        row_count_method="estimate",
        data_bytes=total // 2,
        index_bytes=total // 4,
        toast_bytes=total - total // 2 - total // 4,
        total_bytes=total,
    )


def test_largest_collections_become_size_findings_in_rank_order() -> None:
    measured = [stats(f"t{i}", total=i * 1_000_000) for i in range(1, 9)]

    found = inventory.analyze(measured, top_n=3)

    assert [f.fingerprint for f in found] == ["size:public.t8", "size:public.t7", "size:public.t6"]
    assert {f.category for f in found} == {"size"}
    assert {f.severity for f in found} == {"info"}


def test_size_finding_carries_its_evidence() -> None:
    [top] = inventory.analyze([stats("big", 3_000_000, rows=1234), stats("small", 1_000_000)], 1)

    assert top.subject == "public.big"
    assert top.evidence == {
        "rank": 1,
        "total_bytes": 3_000_000,
        "data_bytes": 1_500_000,
        "index_bytes": 750_000,
        "toast_bytes": 750_000,
        "share_of_total": 0.75,
        "row_count": 1234,
        "row_count_method": "estimate",
    }
    assert "public.big" in top.title and "2.9 MB" in top.title


def test_empty_collections_are_not_reported() -> None:
    assert inventory.analyze([stats("empty", 0)], top_n=5) == []


def test_qualified_name_quotes_only_when_needed() -> None:
    assert CollectionRef("public", "events", CollectionKind.TABLE).qualified == "public.events"
    assert CollectionRef("App", "my table", CollectionKind.TABLE).qualified == '"App"."my table"'
