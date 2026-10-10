"""The interface every analyzer a Run can include satisfies: collect through the SafeExecutor,
analyze, and say what was measured. AnalyzerService.run only loops over analyzers."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta

from db_analyzer.analyzers.workload import MIN_STATS_WINDOW
from db_analyzer.core.model import (
    AnalyzerName,
    CollectionRef,
    FindingCategory,
    GateLimits,
    Observed,
    ProbeResult,
    StorageStats,
    WorkloadReport,
)
from db_analyzer.safety.executor import SafeExecutor


@dataclass(frozen=True)
class RunOptions:
    """What a caller may ask of a Run. Each analyzer accepts only some of these options; asking
    for one that none of the Run's analyzers accepts is an error."""

    collections: Sequence[str] | None = None
    exact_counts: bool = False
    min_stats_window: timedelta = MIN_STATS_WINDOW

    def asked(self) -> set[str]:
        """The options set to something other than their default."""
        default = RunOptions()
        return {f.name for f in fields(self) if getattr(self, f.name) != getattr(default, f.name)}


@dataclass(frozen=True)
class RunContext:
    executor: SafeExecutor
    probe: ProbeResult
    gate: GateLimits
    options: RunOptions


@dataclass(frozen=True)
class Collected:
    """What one analyzer did in a Run.

    `scope` is the collections it measured, or None when it measured nothing (the Run then has
    no scope for it). `skipped` collections were planned but not measured; they are outside the
    scope and make the Run partial, and so does `complete=False` (a workload that ranked
    nothing). `existing` names every relation the Connection has now (collections and indexes,
    qualified) when the analyzer listed them, so Findings on dropped ones become obsolete.
    `storage` and `workload` are the snapshots stored with the Run."""

    found: list[Observed]
    scope: list[CollectionRef] | None
    skipped: list[tuple[CollectionRef, str]] = field(default_factory=list)
    complete: bool = True
    existing: set[str] | None = None
    storage: list[StorageStats] = field(default_factory=list)
    workload: WorkloadReport | None = None


@dataclass(frozen=True)
class Analyzer:
    """One kind of analysis a Run can include. `options` names the RunOptions it accepts.

    `covers` names the categories this analyzer checks completely for every collection in its
    scope, so a Run of it that measured a Finding's collection and did not observe the Finding
    asks "fixed?" (ADR 0011). Leave a category out until its scope says what it checked."""

    name: AnalyzerName
    options: frozenset[str]
    covers: frozenset[FindingCategory]
    measure: Callable[[RunContext], Collected]

    def collect(self, ctx: RunContext) -> Collected:
        """`measure`, with each problem it found marked as covered by this analyzer when its
        category is one the analyzer covers. Facts are never covered."""
        c = self.measure(ctx)
        return replace(c, found=[self._covered(o) for o in c.found])

    def _covered(self, o: Observed) -> Observed:
        covered = o.category in self.covers and not o.is_fact
        return replace(o, covered_by=self.name if covered else None)
