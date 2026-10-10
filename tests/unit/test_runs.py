from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from db_analyzer import runs
from db_analyzer.analyzers import workload
from db_analyzer.core.model import (
    CollectionKind,
    CollectionRef,
    GateLimits,
    Observed,
    Privileges,
    ProbeResult,
    StatsFreshness,
    WorkloadReading,
    WorkloadSource,
    WorkloadStatement,
)
from db_analyzer.runs import Collected, RunContext, RunOptions
from db_analyzer.runs import workload as runs_workload
from db_analyzer.safety.executor import SafeExecutor

EVENTS = CollectionRef("public", "events", CollectionKind.TABLE)
TENANTS = CollectionRef("public", "tenants", CollectionKind.TABLE)


def observed(subject: str) -> Observed:
    return Observed("bloat", subject, "low", f"{subject} is bloated", {}, collection=subject)


def test_analyzers_are_collected_in_registry_order_whatever_the_order_asked() -> None:
    picked = runs.chosen_analyzers(["workload", "inventory"], RunOptions())
    assert [a.name for a in picked] == ["inventory", "workload"]


def test_an_unknown_analyzer_names_the_ones_there_are() -> None:
    with pytest.raises(runs.UnknownAnalyzer, match="Choose from: inventory, workload"):
        runs.chosen_analyzers(["inventory", "hotspot"], RunOptions())


@pytest.mark.parametrize(
    ("names", "options", "refused", "owner"),
    [
        (["workload"], RunOptions(collections=["events"]), ["collections"], "inventory"),
        (
            ["workload"],
            RunOptions(collections=["events"], exact_counts=True),
            ["collections", "exact_counts"],
            "inventory",
        ),
        (
            ["inventory"],
            RunOptions(min_stats_window=timedelta(0)),
            ["min_stats_window"],
            "workload",
        ),
    ],
)
def test_an_option_no_chosen_analyzer_accepts_names_the_one_that_would(
    names: list[str], options: RunOptions, refused: list[str], owner: str
) -> None:
    with pytest.raises(runs.OptionsNotAccepted) as e:
        runs.chosen_analyzers(names, options)
    assert (e.value.options, e.value.analyzer) == (refused, owner)


def test_options_left_at_their_default_are_never_refused() -> None:
    assert runs.chosen_analyzers(["inventory"], RunOptions()) != []


def test_findings_keep_collection_order_and_scope_is_per_analyzer() -> None:
    outcome = runs.combine(
        {
            "inventory": Collected([observed("public.events")], scope=[EVENTS, TENANTS]),
            "workload": Collected([observed("public.tenants")], scope=[]),
        }
    )
    assert [o.subject for o in outcome.found] == ["public.events", "public.tenants"]
    assert outcome.scope == {"inventory": [EVENTS, TENANTS], "workload": []}
    assert outcome.status == "complete"


def test_an_analyzer_that_measured_nothing_has_no_scope() -> None:
    outcome = runs.combine({"workload": Collected([], scope=None, complete=False)})
    assert outcome.scope == {}
    assert outcome.status == "partial"


def test_a_skipped_collection_makes_the_run_partial() -> None:
    outcome = runs.combine(
        {"inventory": Collected([], scope=[EVENTS], skipped=[(TENANTS, "no privilege")])}
    )
    assert outcome.skipped == {"inventory": [(TENANTS, "no privilege")]}
    assert outcome.status == "partial"


def test_existing_is_none_unless_some_analyzer_listed_relations() -> None:
    assert runs.combine({"workload": Collected([], scope=[])}).existing is None
    outcome = runs.combine(
        {
            "inventory": Collected([], scope=[], existing={"public.events"}),
            "workload": Collected([], scope=[]),
        }
    )
    assert outcome.existing == {"public.events"}


def test_snapshots_are_carried_through() -> None:
    report = workload.no_source(["CREATE EXTENSION pg_stat_statements"])
    outcome = runs.combine({"workload": Collected([], scope=[], workload=report)})
    assert outcome.workload is report
    assert outcome.storage == []


# --- The workload analyzer, against fake workload sources ------------------------------------

NOW = datetime(2026, 10, 9, tzinfo=UTC)
SQL = "SELECT * FROM bookings WHERE id = $1"


def probe() -> ProbeResult:
    return ProbeResult(
        server_version_num=170000,
        server_version="17.0",
        in_recovery=False,
        host_type="self_managed",
        extensions={},
        privileges=Privileges(True, True, 0, []),
        settings={},
        stats=StatsFreshness(None, None, 0, None),
        taken_at=NOW,
    )


@dataclass
class FakeSource:
    name: WorkloadSource = "pg_stat_statements"
    steps: list[str] = field(default_factory=list)
    stats_reset: datetime | None = NOW - timedelta(days=3)
    reads: int = 0

    def enable_steps(self, probe: ProbeResult) -> list[str]:
        return self.steps

    def read(self, executor: SafeExecutor, probe: ProbeResult) -> WorkloadReading:
        self.reads += 1
        statement = WorkloadStatement(SQL, 10, 100.0, 10, 0, 0)
        return WorkloadReading([statement], self.stats_reset)


def collect(*sources: FakeSource, window: timedelta = timedelta(hours=1)) -> Collected:
    # Readable sources never touch the executor; only the schema-only review does.
    context = RunContext(
        cast(SafeExecutor, None),
        probe(),
        GateLimits(),
        RunOptions(min_stats_window=window),
    )
    return runs_workload.measure(context, sources)


def test_the_first_readable_source_is_ranked() -> None:
    unreadable, readable = FakeSource(steps=["preload it"]), FakeSource()

    c = collect(unreadable, readable)

    assert (unreadable.reads, readable.reads) == (0, 1)
    assert c.workload is not None and c.workload.source == "pg_stat_statements"
    assert [o.fingerprint for o in c.found] == [f"slow_query:{workload.fingerprint(SQL)}"]
    assert c.scope == [] and c.complete


def test_statistics_younger_than_the_window_are_refused_and_measure_nothing() -> None:
    c = collect(FakeSource(stats_reset=NOW - timedelta(minutes=5)))

    assert c.workload is not None and c.workload.refused is not None
    assert c.found == [] and c.scope is None and not c.complete


# --- Coverage, declared by the analyzer -------------------------------------------------------


def analyzer(*found: Observed) -> runs.Analyzer:
    return runs.Analyzer(
        "inventory",
        options=frozenset(),
        covers=frozenset({"size", "bloat"}),
        measure=lambda ctx: Collected(list(found), scope=[EVENTS]),
    )


def covered_by(*found: Observed) -> dict[str, str | None]:
    context = cast(RunContext, None)  # the stub measures nothing
    return {o.fingerprint: o.covered_by for o in analyzer(*found).collect(context).found}


def test_a_problem_in_a_category_the_analyzer_covers_is_covered_by_it() -> None:
    assert covered_by(observed("public.events")) == {"bloat:public.events": "inventory"}


def test_a_category_the_analyzer_does_not_cover_is_not_covered() -> None:
    proposed = Observed("missing_index", "public.events(tenant_id)", "medium", "t", {})

    assert covered_by(proposed) == {"missing_index:public.events(tenant_id)": None}


def test_facts_are_never_covered_so_dropping_out_of_a_ranking_is_not_a_fix() -> None:
    largest = Observed("size", "public.events", "info", "largest", {}, collection="public.events")
    index_heavy = Observed("size", "public.events", "medium", "t", {}, rule="index_heavy")

    assert covered_by(largest, index_heavy) == {
        "size:public.events": None,
        "size:public.events:index_heavy": "inventory",
    }
