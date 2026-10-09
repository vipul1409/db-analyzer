"""AnalyzerService: the single entry point for the CLI, LangGraph Studio, the API and tests."""

import os
from pathlib import Path

from db_analyzer.adapters.postgres import probe as pg_probe
from db_analyzer.adapters.postgres.session import open_session
from db_analyzer.core.model import (
    AuditEntry,
    Connection,
    ConnectionRefused,
    ProbeResult,
    SessionLimits,
)
from db_analyzer.safety.executor import SafeExecutor
from db_analyzer.store.store import Store


def default_home() -> Path:
    return Path(os.environ.get("DBX_HOME", Path.home() / ".db-analyzer"))


class AnalyzerService:
    def __init__(self, home: Path | None = None):
        self._store = Store((home or default_home()) / "db-analyzer.sqlite")

    def add_connection(
        self, name: str, dsn_env: str, limits: SessionLimits | None = None
    ) -> Connection:
        """Add a Connection, or update the one with this name. `dsn_env` names the env var
        holding the DSN; the DSN itself is never stored. Omitting `limits` keeps the
        Connection's existing safety limits."""
        return self._store.upsert_connection(name, "postgres", dsn_env, limits)

    def probe(self, connection_id: str) -> ProbeResult:
        connection = self._store.get_connection(connection_id)
        dsn = os.environ.get(connection.dsn_env)
        if not dsn:
            raise ConnectionRefused(f"environment variable {connection.dsn_env} is not set")
        with open_session(dsn, connection.limits) as conn:
            executor = SafeExecutor(conn, connection.id, self._store.record_audit)
            result = pg_probe.probe(executor)
        self._store.save_probe(connection.id, result)
        return result

    def audit(self, connection_id: str, thread_id: str | None = None) -> list[AuditEntry]:
        return self._store.audit(connection_id, thread_id)
