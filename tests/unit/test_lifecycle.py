from datetime import UTC, datetime

from db_analyzer.core import lifecycle
from db_analyzer.core.model import (
    AnalyzerName,
    CollectionKind,
    CollectionRef,
    Finding,
    FindingStatus,
    Run,
)

AT = datetime(2026, 10, 9, tzinfo=UTC)
EVENTS = CollectionRef("public", "events", CollectionKind.TABLE)
TENANTS = CollectionRef("public", "tenants", CollectionKind.TABLE)
EVERYTHING = {"public.events", "public.tenants"}


def run(
    scope: list[CollectionRef],
    skipped: list[tuple[CollectionRef, str]] | None = None,
    analyzer: AnalyzerName = "inventory",
) -> Run:
    return Run(
        id="r2",
        connection_id="c",
        thread_id=None,
        scope={analyzer: scope},
        skipped={analyzer: skipped} if skipped else {},
        started_at=AT,
        finished_at=AT,
        status="partial" if skipped else "complete",
    )


def finding(
    fingerprint: str = "bloat:public.events",
    status: FindingStatus = "open",
    unobserved_by: str | None = None,
    collection: str | None = None,
    covered_by: AnalyzerName | None = "inventory",
) -> Finding:
    category, subject = fingerprint.split(":")[:2]
    return Finding(
        connection_id="c",
        fingerprint=fingerprint,
        category=category,  # type: ignore[arg-type]
        subject=subject,
        status=status,
        first_seen_run="r1",
        last_seen_run="r1",
        unobserved_by=unobserved_by,
        collection=collection or subject,
        covered_by=covered_by,
    )


def after(
    r: Run, f: Finding, observed: bool = False, existing: set[str] | None = EVERYTHING
) -> Finding:
    changed = lifecycle.after_run(r, [f], {f.fingerprint} if observed else set(), existing)
    return changed[0] if changed else f


def test_an_acknowledged_finding_stays_acknowledged_when_observed_again() -> None:
    assert after(run([EVENTS]), finding(status="acknowledged"), observed=True).status == (
        "acknowledged"
    )


def test_a_fixed_or_obsolete_finding_seen_again_is_open_again() -> None:
    for status in ("fixed", "obsolete"):
        assert after(run([EVENTS]), finding(status=status), observed=True).status == "open"


def test_a_covered_finding_not_observed_asks_fixed_and_keeps_its_status() -> None:
    for status in ("open", "acknowledged"):
        updated = after(run([EVENTS]), finding(status=status))

        assert updated.status == status
        assert updated.unobserved_by == "r2"


def test_observing_a_finding_again_withdraws_the_fixed_prompt() -> None:
    assert after(run([EVENTS]), finding(unobserved_by="r1"), observed=True).unobserved_by is None


def test_no_fixed_prompt_for_a_subject_outside_the_scope() -> None:
    assert after(run([TENANTS]), finding()).unobserved_by is None


def test_no_fixed_prompt_for_a_skipped_collection() -> None:
    r = run([TENANTS], skipped=[(EVENTS, "total cost 3e+06 > 2e+06")])

    assert after(r, finding()).unobserved_by is None


def test_no_fixed_prompt_from_a_run_of_another_analyzer() -> None:
    assert after(run([EVENTS], analyzer="hotspot"), finding()).unobserved_by is None


def test_an_earlier_fixed_prompt_stands_until_a_covering_run_settles_it() -> None:
    assert after(run([TENANTS]), finding(unobserved_by="r1")).unobserved_by == "r1"


def test_a_finding_no_analyzer_covers_never_asks_fixed() -> None:
    assert after(run([EVENTS]), finding(covered_by=None)).unobserved_by is None


def test_an_uncovered_finding_on_a_dropped_relation_is_still_obsolete() -> None:
    f = finding("size:public.events", covered_by=None)

    assert after(run([TENANTS]), f, existing={"public.tenants"}).status == "obsolete"


def test_size_problems_with_a_rule_do_ask_fixed() -> None:
    updated = after(run([EVENTS]), finding("size:public.events:index_heavy"))

    assert updated.unobserved_by == "r2"


def test_a_finding_on_a_dropped_collection_is_obsolete_without_a_prompt() -> None:
    for status in ("open", "acknowledged", "fixed"):
        updated = after(run([TENANTS]), finding(status=status), existing={"public.tenants"})

        assert (updated.status, updated.unobserved_by) == ("obsolete", None)


def test_only_changed_findings_are_returned() -> None:
    unchanged = finding("bloat:public.tenants")

    assert lifecycle.after_run(run([EVENTS]), [unchanged], set(), EVERYTHING) == []


def test_a_run_that_did_not_list_collections_marks_nothing_obsolete() -> None:
    assert after(run([TENANTS]), finding(), existing=None).status == "open"


UNUSED = "unused_index:public.idx_events_kind"


def test_an_index_finding_is_covered_when_its_table_is_in_scope() -> None:
    f = finding(UNUSED, collection="public.events")
    existing = EVERYTHING | {"public.idx_events_kind"}

    assert after(run([EVENTS]), f, existing=existing).unobserved_by == "r2"
    assert after(run([TENANTS]), f, existing=existing).unobserved_by is None


def test_a_dropped_index_makes_its_finding_obsolete_while_its_table_remains() -> None:
    f = finding(UNUSED, collection="public.events")

    assert after(run([EVENTS]), f, existing=EVERYTHING).status == "obsolete"
    kept = after(run([EVENTS]), f, observed=True, existing=EVERYTHING | {"public.idx_events_kind"})
    assert kept.status == "open"
