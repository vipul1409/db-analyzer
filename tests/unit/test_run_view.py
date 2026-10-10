import json
from dataclasses import replace
from datetime import UTC, datetime

from db_analyzer import report
from db_analyzer.agent import digest
from db_analyzer.analyzers import workload
from db_analyzer.core import run_view
from db_analyzer.core.model import (
    AnalyzerName,
    CollectionKind,
    CollectionRef,
    Connection,
    Observation,
    Run,
    StorageStats,
    WorkloadReport,
)
from db_analyzer.core.run_view import Skipped

AT = datetime(2026, 10, 9, tzinfo=UTC)
SHOP = Connection("c", "shop", "postgres", "DBX_DSN")
MB = 1024**2


def ref(name: str) -> CollectionRef:
    return CollectionRef("public", name, CollectionKind.TABLE)


def stats(name: str, total: int, **skipped: str) -> StorageStats:
    return StorageStats(ref(name), None, "estimate", total, 0, 0, total, skipped=skipped)


def observation(fingerprint: str, severity: str = "medium") -> Observation:
    return Observation("r1", fingerprint, severity, fingerprint, {}, None, None)  # type: ignore[arg-type]


def run(
    scope: dict[AnalyzerName, list[CollectionRef]],
    skipped: dict[AnalyzerName, list[tuple[CollectionRef, str]]] | None = None,
) -> Run:
    return Run("r1", "c", None, scope, skipped or {}, AT, AT, "complete")


def view(
    r: Run,
    observations: list[Observation] = [],  # noqa: B006
    storage: list[StorageStats] = [],  # noqa: B006
    w: WorkloadReport | None = None,
) -> run_view.RunView:
    return run_view.build(SHOP, r, observations, storage, w)


def test_storage_is_ranked_largest_first_then_by_name() -> None:
    v = view(
        run({"inventory": []}),
        storage=[stats("b", MB), stats("c", 5 * MB), stats("a", MB)],
    )

    assert [s.ref.name for s in v.storage or []] == ["c", "a", "b"]


def test_a_run_without_inventory_has_no_storage_section() -> None:
    v = view(run({"workload": []}), w=workload.no_source([]))

    assert v.storage is None
    assert v.sections == ["workload"]


def test_problems_leave_out_facts_and_keep_rank_order() -> None:
    v = view(
        run({"inventory": []}),
        [observation("bloat:a", "high"), observation("size:a", "info"), observation("bloat:b")],
    )

    assert [o.fingerprint for o in v.problems] == ["bloat:a", "bloat:b"]


def test_skipped_lists_whole_collections_then_measurements_by_size() -> None:
    v = view(
        run({"inventory": [ref("a"), ref("b")]}, {"hotspot": [(ref("x"), "refused")]}),
        storage=[stats("a", MB, exact_count="gate"), stats("b", 2 * MB, dead_tuple_scan="cap")],
    )

    assert v.skipped == [
        Skipped("hotspot", "public.x", None, "refused"),
        Skipped("inventory", "public.b", "dead_tuple_scan", "cap"),
        Skipped("inventory", "public.a", "exact_count", "gate"),
    ]


def test_markdown_is_titled_by_the_first_section_and_lists_what_was_skipped() -> None:
    v = view(
        run({"inventory": [ref("a")]}, {"hotspot": [(ref("x"), "refused")]}),
        storage=[stats("a", MB, exact_count="gate")],
        w=workload.no_source(["enable it"]),
    )

    md = report.markdown(v)

    assert md.startswith("# Inventory report: shop")
    assert "## Workload" in md
    assert "- hotspot: public.x: refused" in md
    assert "- public.a: exact count skipped: gate" in md


def test_json_lists_collections_by_name_and_findings_by_fingerprint() -> None:
    v = view(
        run({"inventory": []}),
        [observation("size:b", "info"), observation("bloat:a")],
        storage=[stats("b", 2 * MB), stats("a", MB)],
    )

    doc = json.loads(report.json_export(v))

    assert [c["collection"] for c in doc["collections"]] == ["public.a", "public.b"]
    assert [f["fingerprint"] for f in doc["findings"]] == ["bloat:a", "size:b"]


def test_the_agent_digest_shows_problems_and_every_skip() -> None:
    v = view(
        run({"inventory": [ref("a")]}, {"hotspot": [(ref("x"), "refused")]}),
        [observation("size:a", "info"), observation("bloat:a")],
        storage=[replace(stats("a", MB), skipped={"exact_count": "gate"})],
    )

    d = digest.storage(v, top_n=5)

    assert [f["finding"] for f in d["findings"]] == ["bloat:a"]
    assert d["skipped"] == [
        {"table": "public.x", "measurement": None, "reason": "refused"},
        {"table": "public.a", "measurement": "exact_count", "reason": "gate"},
    ]
