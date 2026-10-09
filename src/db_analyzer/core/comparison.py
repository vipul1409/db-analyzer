"""Run comparison: what changed between two Runs, over the scope they share only, so a partial
or targeted Run never looks like a regression or a fix."""

from dataclasses import dataclass

from db_analyzer.core.lifecycle import evaluated_by
from db_analyzer.core.model import AnalyzerName, Finding, Run, StorageStats


@dataclass(frozen=True)
class SizeChange:
    collection: str
    before_bytes: int
    after_bytes: int
    delta_bytes: int


@dataclass(frozen=True)
class RunComparison:
    """`shared` is, per analyzer, the collections both Runs measured; `not_compared` those only
    one of them measured (including ones the other skipped). Size changes are largest first;
    `appeared` and `disappeared` are problem Findings, by fingerprint."""

    before: str
    after: str
    shared: dict[AnalyzerName, list[str]]
    not_compared: dict[AnalyzerName, list[str]]
    size_changes: list[SizeChange]
    appeared: list[str]
    disappeared: list[str]


def compare(
    a: Run,
    b: Run,
    a_stats: list[StorageStats],
    b_stats: list[StorageStats],
    a_seen: list[Finding],
    b_seen: list[Finding],
) -> RunComparison:
    """Compare two Runs of one Connection, oldest first whatever the argument order. `*_stats`
    are the sizes each Run measured; `*_seen` the Findings each observed."""
    if a.started_at > b.started_at:
        a, b, a_stats, b_stats, a_seen, b_seen = b, a, b_stats, a_stats, b_seen, a_seen
    shared: dict[AnalyzerName, list[str]] = {}
    not_compared: dict[AnalyzerName, list[str]] = {}
    for analyzer in sorted(a.scope.keys() | b.scope.keys()):
        in_a, in_b = a.in_scope(analyzer), b.in_scope(analyzer)
        if both := in_a & in_b:
            shared[analyzer] = sorted(both)
        if either := in_a ^ in_b:
            not_compared[analyzer] = sorted(either)

    inventory = set(shared.get("inventory", []))
    sizes_a = {s.ref.qualified: s.total_bytes for s in a_stats}
    sizes_b = {s.ref.qualified: s.total_bytes for s in b_stats}
    changes = [
        SizeChange(name, sizes_a[name], sizes_b[name], sizes_b[name] - sizes_a[name])
        for name in sorted(inventory & sizes_a.keys() & sizes_b.keys())
    ]

    def problems(seen: list[Finding]) -> set[str]:
        return {
            f.fingerprint
            for f in seen
            if (analyzer := evaluated_by(f)) is not None
            and f.collection in shared.get(analyzer, [])
        }

    before, after = problems(a_seen), problems(b_seen)
    return RunComparison(
        before=a.id,
        after=b.id,
        shared=shared,
        not_compared=not_compared,
        size_changes=sorted(changes, key=lambda c: -abs(c.delta_bytes)),
        appeared=sorted(after - before),
        disappeared=sorted(before - after),
    )
