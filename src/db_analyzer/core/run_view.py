"""One Run as every reader shows it: the Markdown and JSON reports, the CLI and the agent's
digest only format a RunView, so ranking, the problem/fact split and what was skipped are
decided once."""

from dataclasses import dataclass

from db_analyzer.core.model import (
    AnalyzerName,
    Connection,
    Observation,
    Run,
    StorageStats,
    WorkloadReport,
)


@dataclass(frozen=True)
class Skipped:
    """A collection a Run planned but did not measure (`measurement` None), or one measurement it
    did not take on a collection it did measure."""

    analyzer: AnalyzerName
    collection: str
    measurement: str | None
    reason: str


@dataclass(frozen=True)
class RunView:
    """`observations` in rank order, most severe first. `storage` is None when the Run did not
    include inventory, else largest first; `workload` is None when it did not include workload.
    `skipped` lists whole collections first, then measurements in `storage` order."""

    connection: Connection
    run: Run
    observations: list[Observation]
    storage: list[StorageStats] | None
    workload: WorkloadReport | None
    skipped: list[Skipped]

    @property
    def problems(self) -> list[Observation]:
        return [o for o in self.observations if not o.is_fact]

    @property
    def sections(self) -> list[AnalyzerName]:
        """The analyzers with a section of their own, in collection order."""
        shown: list[tuple[AnalyzerName, object]] = [
            ("inventory", self.storage),
            ("workload", self.workload),
        ]
        return [name for name, section in shown if section is not None]


def build(
    connection: Connection,
    run: Run,
    observations: list[Observation],
    storage: list[StorageStats],
    workload: WorkloadReport | None,
) -> RunView:
    ranked = (
        sorted(storage, key=lambda s: (-s.total_bytes, s.ref.qualified))
        if "inventory" in run.scope
        else None
    )
    return RunView(
        connection=connection,
        run=run,
        observations=observations,
        storage=ranked,
        workload=workload,
        skipped=[
            Skipped(analyzer, ref.qualified, None, why)
            for analyzer, items in run.skipped.items()
            for ref, why in items
        ]
        + [
            Skipped("inventory", s.ref.qualified, what, why)
            for s in ranked or []
            for what, why in s.skipped.items()
        ],
    )
