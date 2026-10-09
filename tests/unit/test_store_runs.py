from datetime import UTC, datetime
from pathlib import Path

import pytest

from db_analyzer.core.model import (
    CollectionKind,
    CollectionRef,
    DeadTupleScan,
    GateLimits,
    Maintenance,
    Observed,
    StorageStats,
)
from db_analyzer.store.store import Store

EVENTS = CollectionRef("public", "events", CollectionKind.TABLE)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "s.sqlite")


@pytest.fixture
def connection_id(store: Store) -> str:
    return store.upsert_connection("prod", "postgres", "DSN", None, None).id


def size(rank: int) -> Observed:
    return Observed("size", "public.events", "info", f"#{rank}", {"rank": rank})


def test_run_lifecycle_records_scope_and_status(store: Store, connection_id: str) -> None:
    run = store.start_run(connection_id, thread_id=None)
    assert run.status == "running" and run.finished_at is None

    store.finish_run(run.id, "complete", scope={"inventory": [EVENTS]}, skipped={})

    [saved] = store.runs(connection_id)
    assert saved.id == run.id
    assert saved.status == "complete"
    assert saved.scope == {"inventory": [EVENTS]}
    assert saved.finished_at is not None


def test_same_subject_in_two_runs_is_one_finding_with_two_observations(
    store: Store, connection_id: str
) -> None:
    first = store.start_run(connection_id, None)
    store.record_observations(connection_id, first.id, [size(1)])
    second = store.start_run(connection_id, None)
    store.record_observations(connection_id, second.id, [size(2)])

    [finding] = store.findings(connection_id)
    assert finding.fingerprint == "size:public.events"
    assert finding.status == "open"
    assert (finding.first_seen_run, finding.last_seen_run) == (first.id, second.id)
    observations = store.observations(connection_id, "size:public.events")
    assert [(o.run_id, o.evidence) for o in observations] == [
        (first.id, {"rank": 1}),
        (second.id, {"rank": 2}),
    ]


def test_findings_are_per_connection(store: Store, connection_id: str) -> None:
    other = store.upsert_connection("staging", "postgres", "DSN2", None, None).id
    store.record_observations(connection_id, store.start_run(connection_id, None).id, [size(1)])

    assert store.findings(other) == []


def test_snapshots_round_trip(store: Store, connection_id: str) -> None:
    run = store.start_run(connection_id, None)
    measured = [StorageStats(EVENTS, 10, "estimate", 100, 20, 5, 125)]

    store.save_snapshots(run.id, measured)

    assert store.snapshots(run.id) == measured


def test_snapshots_round_trip_activity_scans_and_skipped_measurements(
    store: Store, connection_id: str
) -> None:
    run = store.start_run(connection_id, None)
    at = datetime(2026, 10, 1, 12, tzinfo=UTC)
    measured = [
        StorageStats(
            EVENTS,
            10,
            "exact",
            100,
            20,
            5,
            125,
            partitions=4,
            maintenance=Maintenance(10, 3, 2, at, None, autovacuum_disabled=True),
            dead_tuple_scan=DeadTupleScan(10, 3, 12.5, 4.0),
            skipped={"exact_count": "total cost 3e+06 > 2e+06"},
        )
    ]

    store.save_snapshots(run.id, measured)

    assert store.snapshots(run.id) == measured


def test_gate_limits_persist_and_are_kept_when_omitted(store: Store) -> None:
    first = store.upsert_connection("prod", "postgres", "DSN", None, GateLimits(max_total_cost=5))

    again = store.upsert_connection("prod", "postgres", "DSN", None, None)

    assert again.gate == first.gate == GateLimits(max_total_cost=5)
