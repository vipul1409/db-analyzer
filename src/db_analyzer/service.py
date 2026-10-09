"""AnalyzerService: the single entry point for the CLI, LangGraph Studio, the API and tests."""

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from db_analyzer import report
from db_analyzer.adapters.postgres import inventory as pg_inventory
from db_analyzer.adapters.postgres import probe as pg_probe
from db_analyzer.adapters.postgres.session import open_session
from db_analyzer.analyzers import inventory
from db_analyzer.core.model import (
    AnalyzerName,
    AuditEntry,
    Connection,
    ConnectionRefused,
    Finding,
    GateLimits,
    Observation,
    ProbeResult,
    Run,
    SessionLimits,
    StorageStats,
)
from db_analyzer.safety.executor import SafeExecutor
from db_analyzer.store.store import Store

SUPPORTED_ANALYZERS: tuple[AnalyzerName, ...] = ("inventory",)


def default_home() -> Path:
    return Path(os.environ.get("DBX_HOME", Path.home() / ".db-analyzer"))


class AnalyzerService:
    def __init__(self, home: Path | None = None):
        self._store = Store((home or default_home()) / "db-analyzer.sqlite")

    def add_connection(
        self,
        name: str,
        dsn_env: str,
        limits: SessionLimits | None = None,
        gate: GateLimits | None = None,
    ) -> Connection:
        """Add a Connection, or update the one with this name. `dsn_env` names the env var
        holding the DSN; the DSN itself is never stored. Omitting `limits` or `gate` keeps the
        Connection's existing safety limits."""
        return self._store.upsert_connection(name, "postgres", dsn_env, limits, gate)

    def connection(self, name: str) -> Connection:
        return self._store.find_connection(name)

    def probe(self, connection_id: str) -> ProbeResult:
        connection = self._store.get_connection(connection_id)
        with self._executor(connection) as executor:
            result = pg_probe.probe(executor)
        self._store.save_probe(connection.id, result)
        return result

    def run(
        self,
        connection_id: str,
        analyzers: Sequence[AnalyzerName] = SUPPORTED_ANALYZERS,
        thread_id: str | None = None,
    ) -> Run:
        """A deterministic Run, no LLM: probe, measure, record Findings and Observations."""
        if unsupported := set(analyzers) - set(SUPPORTED_ANALYZERS):
            raise ValueError(f"analyzers not available yet: {sorted(unsupported)}")
        connection = self._store.get_connection(connection_id)
        run = self._store.start_run(connection.id, thread_id)
        try:
            with self._executor(connection) as executor:
                probe = pg_probe.probe(executor)
                measured = pg_inventory.storage_stats(executor, probe.server_version_num)
            self._store.save_probe(connection.id, probe)
            self._store.save_snapshots(run.id, measured)
            self._store.record_observations(connection.id, run.id, inventory.analyze(measured))
        except BaseException:
            self._store.finish_run(run.id, "failed", scope={}, skipped={})
            raise
        return self._store.finish_run(
            run.id, "complete", scope={"inventory": [m.ref for m in measured]}, skipped={}
        )

    def runs(self, connection_id: str) -> list[Run]:
        return self._store.runs(connection_id)

    def storage(self, run_id: str) -> list[StorageStats]:
        """Collection sizes measured by a Run."""
        return self._store.snapshots(run_id)

    def findings(self, connection_id: str) -> list[Finding]:
        return self._store.findings(connection_id)

    def observations(self, connection_id: str, fingerprint: str) -> list[Observation]:
        return self._store.observations(connection_id, fingerprint)

    def export(self, run_id: str, fmt: Literal["md"] = "md") -> bytes:
        run = self._store.get_run(run_id)
        connection = self._store.get_connection(run.connection_id)
        text = report.markdown(
            connection, run, self._store.snapshots(run_id), self._store.run_observations(run_id)
        )
        return text.encode()

    def audit(self, connection_id: str, thread_id: str | None = None) -> list[AuditEntry]:
        return self._store.audit(connection_id, thread_id)

    @contextmanager
    def _executor(self, connection: Connection) -> Iterator[SafeExecutor]:
        dsn = os.environ.get(connection.dsn_env)
        if not dsn:
            raise ConnectionRefused(f"environment variable {connection.dsn_env} is not set")
        with open_session(dsn, connection.limits) as conn:
            yield SafeExecutor(conn, connection.id, self._store.record_audit, connection.gate)
