from pathlib import Path

import pytest

from db_analyzer.service import AnalyzerService

from .conftest import SUPPORTED, DsnEnv, dsn

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("major", SUPPORTED)
def test_probe_reports_extensions_privileges_settings_and_stats(
    service: AnalyzerService, dsn_env: DsnEnv, major: int
) -> None:
    conn = service.add_connection(name="app", dsn_env=dsn_env(major, "db_analyzer"))

    probe = service.probe(conn.id)

    assert {"pg_stat_statements", "hypopg", "pgstattuple"} <= probe.extensions.keys()
    assert probe.privileges.pg_monitor is True
    assert "private.notes" in probe.privileges.unreadable_tables
    assert probe.settings["track_io_timing"] == "on"
    assert probe.settings["pg_stat_statements.track"] == "all"
    assert probe.stats.statements_stats_reset is not None


@pytest.mark.parametrize("major", SUPPORTED)
def test_every_probe_statement_is_audited(
    service: AnalyzerService, dsn_env: DsnEnv, major: int
) -> None:
    conn = service.add_connection(name="app", dsn_env=dsn_env(major, "db_analyzer"))

    service.probe(conn.id)

    entries = service.audit(conn.id)
    assert len(entries) >= 5
    assert all(e.decision == "executed" and e.purpose == "probe" for e in entries)
    assert all(e.row_count is not None and e.duration_ms is not None for e in entries)


def test_credentials_are_never_stored(
    tmp_path: Path, service: AnalyzerService, dsn_env: DsnEnv
) -> None:
    major = SUPPORTED[0]
    conn = service.add_connection(name="app", dsn_env=dsn_env(major, "db_analyzer"))
    service.probe(conn.id)

    stored = (tmp_path / "db-analyzer.sqlite").read_bytes()
    assert dsn(major).encode() not in stored
    assert b"db_analyzer@" not in stored
    assert b":db_analyzer" not in stored
