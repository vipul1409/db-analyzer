"""The analyzers a Run can include, and how their results make one Run."""

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from db_analyzer.core import lifecycle
from db_analyzer.core.model import (
    AnalyzerName,
    CollectionRef,
    Connection,
    Finding,
    FindingStatus,
    Observed,
    ProbeResult,
    Run,
    RunStatus,
    StorageStats,
    WorkloadReport,
)
from db_analyzer.runs import inventory, workload
from db_analyzer.runs.base import Analyzer, Collected, RunContext, RunOptions, Scope, Skipped

__all__ = [
    "ANALYZERS",
    "DEFAULT",
    "Analyzer",
    "Collect",
    "Collected",
    "Outcome",
    "RunContext",
    "RunOptions",
    "RunStore",
    "chosen_analyzers",
    "combine",
    "execute",
]

logger = logging.getLogger(__name__)

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


# What produces a Run's results: probe the Connection and collect with each analyzer. It is the
# only part of a Run that touches the database, so passing a canned one drives `execute` with no
# database at all.
Collect = Callable[[], tuple[ProbeResult, Outcome]]


class RunStore(Protocol):
    """The slice of the store a Run transaction writes through. The real Store satisfies it; a
    fake lets the whole transaction be tested without a database."""

    def start_run(self, connection_id: str, thread_id: str | None) -> Run: ...
    def save_probe(self, connection_id: str, result: ProbeResult) -> None: ...
    def save_snapshots(self, run_id: str, measured: list[StorageStats]) -> None: ...
    def save_workload(self, run_id: str, report: WorkloadReport) -> None: ...
    def record_observations(
        self, connection_id: str, run_id: str, observed: list[Observed]
    ) -> None: ...
    def finish_run(self, run_id: str, status: RunStatus, scope: Scope, skipped: Skipped) -> Run: ...
    def findings(
        self, connection_id: str, statuses: Collection[FindingStatus] | None = None
    ) -> list[Finding]: ...
    def update_findings(self, findings: list[Finding]) -> None: ...


def execute(
    store: RunStore, connection: Connection, collect: Collect, thread_id: str | None = None
) -> Run:
    """Run one Run as a single transaction: start it, collect (the only database work), persist
    the probe, snapshots, workload and observations in that order, finish it, then move the
    Connection's Findings through their lifecycle.

    On any error the Run is finished `failed` with no scope and the error re-raised, so a failed
    Run leaves nothing half-written and the lifecycle never runs on it. Returns the finished
    Run."""
    run = store.start_run(connection.id, thread_id)
    logger.info("run %s start: connection=%s thread=%s", run.id, connection.name, thread_id)
    try:
        probe, outcome = collect()
        store.save_probe(connection.id, probe)
        store.save_snapshots(run.id, outcome.storage)
        if outcome.workload is not None:
            store.save_workload(run.id, outcome.workload)
        store.record_observations(connection.id, run.id, outcome.found)
    except BaseException:
        logger.exception("run %s failed", run.id)
        store.finish_run(run.id, "failed", scope={}, skipped={})
        raise
    if outcome.workload is not None and outcome.workload.refused:
        logger.warning("run %s workload refused: %s", run.id, outcome.workload.refused)
    run = store.finish_run(run.id, outcome.status, scope=outcome.scope, skipped=outcome.skipped)
    logger.info("run %s finished: status=%s", run.id, run.status)
    changed = lifecycle.after_run(
        run,
        store.findings(connection.id),
        {o.fingerprint for o in outcome.found},
        existing=outcome.existing,
    )
    store.update_findings(changed)
    return run
