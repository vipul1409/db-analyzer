"""The Run transaction (runs.execute) driven against a fake store, no database. The only
database work is the injected `collect`; everything else is store calls and the pure lifecycle,
so the whole transaction — including the failure path — is unit-testable here."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from db_analyzer import runs
from db_analyzer.analyzers import workload
from db_analyzer.core.model import (
    CollectionKind,
    CollectionRef,
    Connection,
    Finding,
    FindingStatus,
    Observed,
    Privileges,
    ProbeResult,
    Run,
    RunStatus,
    StatsFreshness,
    StorageStats,
    WorkloadReport,
)
from db_analyzer.runs import Outcome
from db_analyzer.runs.base import Scope, Skipped

EVENTS = CollectionRef("public", "events", CollectionKind.TABLE)
CONNECTION = Connection(id="c1", name="shop", adapter_kind="postgres", dsn_env="SHOP_DSN")
NOW = datetime(2026, 10, 21, tzinfo=UTC)


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


def observed(subject: str) -> Observed:
    return Observed(
        "bloat", subject, "low", f"{subject} is bloated", {}, collection=subject, covered_by=None
    )


def outcome(
    *,
    found: list[Observed] | None = None,
    scope: Scope | None = None,
    skipped: Skipped | None = None,
    status: RunStatus = "complete",
    existing: set[str] | None = None,
    storage: list[StorageStats] | None = None,
    workload: WorkloadReport | None = None,
) -> Outcome:
    return Outcome(
        found=found or [],
        scope=scope or {},
        skipped=skipped or {},
        status=status,
        existing=existing,
        storage=storage or [],
        workload=workload,
    )


def finding(subject: str, *, status: FindingStatus = "open") -> Finding:
    return Finding(
        connection_id=CONNECTION.id,
        fingerprint=f"bloat:{subject}",
        category="bloat",
        subject=subject,
        status=status,
        first_seen_run="r0",
        last_seen_run="r0",
        collection=subject,
        covered_by="inventory",
    )


@dataclass
class FakeStore:
    """Records what a Run transaction writes, in order, and hands back the Findings it is given."""

    existing: list[Finding] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    saved_probe: ProbeResult | None = None
    saved_snapshots: list[StorageStats] | None = None
    saved_workload: WorkloadReport | None = None
    recorded: list[Observed] | None = None
    updated: list[Finding] | None = None
    finished: list[tuple[RunStatus, Scope, Skipped]] = field(default_factory=list)

    def start_run(self, connection_id: str, thread_id: str | None) -> Run:
        self.calls.append("start_run")
        return Run("run-1", connection_id, thread_id, {}, {}, NOW, None, "running")

    def save_probe(self, connection_id: str, result: ProbeResult) -> None:
        self.calls.append("save_probe")
        self.saved_probe = result

    def save_snapshots(self, run_id: str, measured: list[StorageStats]) -> None:
        self.calls.append("save_snapshots")
        self.saved_snapshots = measured

    def save_workload(self, run_id: str, report: WorkloadReport) -> None:
        self.calls.append("save_workload")
        self.saved_workload = report

    def record_observations(
        self, connection_id: str, run_id: str, observed: list[Observed]
    ) -> None:
        self.calls.append("record_observations")
        self.recorded = observed

    def finish_run(self, run_id: str, status: RunStatus, scope: Scope, skipped: Skipped) -> Run:
        self.calls.append("finish_run")
        self.finished.append((status, scope, skipped))
        return Run(run_id, CONNECTION.id, None, scope, skipped, NOW, NOW, status)

    def findings(self, connection_id: str, statuses: object = None) -> list[Finding]:
        return self.existing

    def update_findings(self, findings: list[Finding]) -> None:
        self.calls.append("update_findings")
        self.updated = findings


def run_with(store: FakeStore, out: Outcome, *, probe_result: ProbeResult | None = None) -> Run:
    taken = probe_result or probe()
    return runs.execute(store, CONNECTION, lambda: (taken, out))


def test_a_complete_run_persists_in_order_and_finishes_complete() -> None:
    store = FakeStore()
    stat = StorageStats(EVENTS, None, "estimate", 0, 0, None, 1)
    found, snaps = [observed("public.events")], [stat]

    run = run_with(store, outcome(found=found, scope={"inventory": [EVENTS]}, storage=snaps))

    assert store.calls == [
        "start_run",
        "save_probe",
        "save_snapshots",
        "record_observations",
        "finish_run",
        "update_findings",
    ]
    assert store.saved_probe is not None and store.saved_snapshots == snaps
    assert store.recorded == found
    assert run.status == "complete"


def test_a_partial_run_finishes_partial() -> None:
    store = FakeStore()

    run = run_with(store, outcome(status="partial", skipped={"inventory": [(EVENTS, "no priv")]}))

    assert run.status == "partial"
    assert store.finished == [("partial", {}, {"inventory": [(EVENTS, "no priv")]})]


def test_an_unranked_workload_run_is_saved_and_finishes_partial() -> None:
    # The other way a Run goes partial (ADR 0008/0010): the workload could not be ranked. The
    # seam persists the refused report and finishes the Run partial, and logs the refusal.
    refused = WorkloadReport(
        source="pg_stat_statements",
        stats_reset=None,
        window_seconds=None,
        items=[],
        statements=0,
        total_ms=0.0,
        excluded={},
        warnings=["statistics too young"],
        refused="statistics too young to rank",
    )
    store = FakeStore()

    run = run_with(store, outcome(status="partial", workload=refused))

    assert run.status == "partial"
    assert store.saved_workload is refused


def test_workload_is_saved_only_when_present() -> None:
    no_workload = FakeStore()
    run_with(no_workload, outcome())
    assert "save_workload" not in no_workload.calls

    report = workload.no_source(["CREATE EXTENSION pg_stat_statements"])
    with_workload = FakeStore()
    run_with(with_workload, outcome(workload=report))
    assert with_workload.saved_workload is report


def test_a_failure_in_collect_finishes_the_run_failed_and_re_raises() -> None:
    store = FakeStore()

    def boom() -> tuple[ProbeResult, Outcome]:
        raise RuntimeError("analyzer blew up")

    with pytest.raises(RuntimeError, match="analyzer blew up"):
        runs.execute(store, CONNECTION, boom)

    assert store.finished == [("failed", {}, {})]
    assert "record_observations" not in store.calls  # nothing persisted past the failure
    assert "update_findings" not in store.calls  # the lifecycle never runs on a failed Run


def test_the_lifecycle_runs_on_the_connections_findings() -> None:
    # A Finding the Run covers (inventory measured its collection) but did not observe asks
    # "fixed?": its unobserved_by is set to this Run.
    store = FakeStore(existing=[finding("public.events")])

    run = run_with(
        store,
        outcome(found=[], scope={"inventory": [EVENTS]}, existing={"public.events"}),
    )

    assert store.updated is not None and len(store.updated) == 1
    assert store.updated[0].unobserved_by == run.id
