"""The analyzers a Run can include, and how their results make one Run."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from db_analyzer.core.model import (
    AnalyzerName,
    CollectionRef,
    Observed,
    RunStatus,
    StorageStats,
    WorkloadReport,
)
from db_analyzer.runs import inventory, workload
from db_analyzer.runs.base import Analyzer, Collected, RunContext, RunOptions

__all__ = [
    "ANALYZERS",
    "DEFAULT",
    "Analyzer",
    "Collected",
    "Outcome",
    "RunContext",
    "RunOptions",
    "chosen_analyzers",
    "combine",
]

# In the order a Run collects them, which is also the rank order of its Findings.
ANALYZERS: dict[AnalyzerName, Analyzer] = {
    a.name: a for a in (inventory.ANALYZER, workload.ANALYZER)
}
DEFAULT: tuple[AnalyzerName, ...] = ("inventory",)


class UnknownAnalyzer(ValueError):
    def __init__(self, name: str):
        super().__init__(f"Unknown analyzer {name!r}. Choose from: {', '.join(ANALYZERS)}")
        self.name = name


class OptionsNotAccepted(ValueError):
    """Options asked for that none of the Run's analyzers accepts, with the analyzer that
    would."""

    def __init__(self, options: list[str], analyzer: AnalyzerName):
        verb = "applies" if len(options) == 1 else "apply"
        super().__init__(f"{' and '.join(options)} {verb} to the {analyzer} analyzer")
        self.options = options
        self.analyzer = analyzer


def chosen_analyzers(names: Sequence[str], options: RunOptions) -> list[Analyzer]:
    """The analyzers named, in collection order, once each accepts every option asked for."""
    if unknown := [n for n in names if n not in ANALYZERS]:
        raise UnknownAnalyzer(unknown[0])
    picked = [a for name, a in ANALYZERS.items() if name in names]
    accepted = set().union(*(a.options for a in picked))
    for a in ANALYZERS.values():
        if refused := sorted((options.asked() - accepted) & a.options):
            raise OptionsNotAccepted(refused, a.name)
    return picked


@dataclass(frozen=True)
class Outcome:
    """A Run's analyzers' results, combined: what to record and how the Run finishes."""

    found: list[Observed]
    scope: dict[AnalyzerName, list[CollectionRef]]
    skipped: dict[AnalyzerName, list[tuple[CollectionRef, str]]]
    status: RunStatus
    existing: set[str] | None
    storage: list[StorageStats]
    workload: WorkloadReport | None


def combine(results: Mapping[AnalyzerName, Collected]) -> Outcome:
    """Partial when any analyzer skipped a collection or did not complete. `existing` is None
    unless some analyzer listed the Connection's relations."""
    listings = [c.existing for c in results.values() if c.existing is not None]
    reports = [c.workload for c in results.values() if c.workload is not None]
    complete = all(c.complete and not c.skipped for c in results.values())
    return Outcome(
        found=[o for c in results.values() for o in c.found],
        scope={a: c.scope for a, c in results.items() if c.scope is not None},
        skipped={a: c.skipped for a, c in results.items() if c.skipped},
        status="complete" if complete else "partial",
        existing=set().union(*listings) if listings else None,
        storage=[s for c in results.values() for s in c.storage],
        workload=reports[0] if reports else None,
    )
