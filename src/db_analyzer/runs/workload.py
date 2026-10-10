"""The workload analyzer in a Run: rank the most expensive statements from the first workload
source the Connection can read and explain each from its generic plan, or, with none, review the
schema instead and say how to enable one (ADR 0010)."""

from collections.abc import Callable, Sequence
from typing import Protocol

from db_analyzer.adapters.postgres import plans as pg_plans
from db_analyzer.adapters.postgres import workload as pg_workload
from db_analyzer.analyzers import workload
from db_analyzer.analyzers.plan_rules import PlanContext
from db_analyzer.core.model import (
    ProbeResult,
    StatementPlan,
    WorkloadReading,
    WorkloadReport,
    WorkloadSource,
)
from db_analyzer.runs.base import Analyzer, Collected, RunContext
from db_analyzer.safety.executor import SafeExecutor


class Source(Protocol):
    """Where a Connection's record of executed statements comes from."""

    @property
    def name(self) -> WorkloadSource: ...

    def enable_steps(self, probe: ProbeResult) -> list[str]:
        """What to do, in order, to make the source readable; empty when it already is."""
        ...

    def read(self, executor: SafeExecutor, probe: ProbeResult) -> WorkloadReading: ...


# Preferred first: a Run reads the first one the Connection can, and with none, shows how to
# enable the first.
SOURCES: tuple[Source, ...] = (pg_workload.PG_STAT_STATEMENTS,)


# Explains ranked statements: their plans by fingerprint, and the catalog context to read them.
Planner = Callable[
    [SafeExecutor, ProbeResult, WorkloadReport], tuple[dict[str, StatementPlan], PlanContext]
]


def measure(
    ctx: RunContext, sources: Sequence[Source] = SOURCES, planner: Planner | None = None
) -> Collected:
    """Ranks nothing when the statistics are younger than `min_stats_window`. A Run whose
    workload was refused, or reviewed from the schema alone, is partial. `planner` explains the
    ranked statements (default: Postgres generic plans)."""
    executor, probe = ctx.executor, ctx.probe
    steps = [s.enable_steps(probe) for s in sources]
    readable = [s for s, missing in zip(sources, steps, strict=True) if not missing]
    if not readable:
        foreign_keys = pg_workload.unindexed_foreign_keys(executor, probe.server_version_num)
        scans = pg_workload.scan_activity(executor, probe.server_version_num)
        return Collected(
            found=workload.schema_only_review(foreign_keys, scans),
            scope=[s.table for s in scans],
            complete=False,
            workload=workload.no_source(steps[0] if steps else []),
        )
    source = readable[0]
    reading = source.read(executor, probe)
    report = workload.report(
        reading.statements,
        source=source.name,
        stats_reset=reading.stats_reset,
        now=probe.taken_at,
        on_replica=probe.in_recovery,
        min_window=ctx.options.min_stats_window,
    )
    return Collected(
        found=workload.analyze(report, *(planner or plan_statements)(executor, probe, report)),
        # Statements name no collections, so a ranked workload has an empty list of them.
        scope=None if report.refused else [],
        complete=report.refused is None,
        workload=report,
    )


def plan_statements(
    executor: SafeExecutor, probe: ProbeResult, report: WorkloadReport
) -> tuple[dict[str, StatementPlan], PlanContext]:
    """The generic plan of every ranked statement, and what the catalog says about the
    relations they read. Nothing is executed (ADR 0002); a statement that cannot be planned
    keeps its Finding, with the reason in place of a plan."""
    if not report.items:
        return {}, PlanContext(relations={})
    version = probe.server_version_num
    context = PlanContext(
        relations={r.name: r for r in pg_plans.relation_estimates(executor, version)},
        unindexed_foreign_keys=pg_workload.unindexed_foreign_keys(executor, version),
    )
    return {i.fingerprint: pg_plans.generic_plan(executor, i.text) for i in report.items}, context


# Covers nothing yet: a statement missing from a later top 25 isn't fixed, and the schema-only
# review's missing_index Findings wait for index advice to say what its scope covers (ADR 0010).
ANALYZER = Analyzer(
    "workload", options=frozenset({"min_stats_window"}), covers=frozenset(), measure=measure
)
