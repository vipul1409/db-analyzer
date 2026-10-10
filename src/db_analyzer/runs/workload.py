"""The workload analyzer in a Run: rank the most expensive statements from the first workload
source the Connection can read, or, with none, review the schema instead and say how to enable
one (ADR 0010)."""

from collections.abc import Sequence
from typing import Protocol

from db_analyzer.adapters.postgres import workload as pg_workload
from db_analyzer.analyzers import workload
from db_analyzer.core.model import ProbeResult, WorkloadReading, WorkloadSource
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


def measure(ctx: RunContext, sources: Sequence[Source] = SOURCES) -> Collected:
    """Ranks nothing when the statistics are younger than `min_stats_window`. A Run whose
    workload was refused, or reviewed from the schema alone, is partial."""
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
        found=workload.analyze(report),
        # Statements name no collections, so a ranked workload has an empty list of them.
        scope=None if report.refused else [],
        complete=report.refused is None,
        workload=report,
    )


# Covers nothing yet: a statement missing from a later top 25 isn't fixed, and the schema-only
# review's missing_index Findings wait for index advice to say what its scope covers (ADR 0010).
ANALYZER = Analyzer(
    "workload", options=frozenset({"min_stats_window"}), covers=frozenset(), measure=measure
)
