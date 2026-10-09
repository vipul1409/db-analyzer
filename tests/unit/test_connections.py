from pathlib import Path

from db_analyzer.core.model import SessionLimits
from db_analyzer.service import AnalyzerService


def test_reconnecting_without_limits_keeps_existing_limits(tmp_path: Path) -> None:
    service = AnalyzerService(home=tmp_path)
    first = service.add_connection("prod", "DSN_A", SessionLimits(statement_timeout="5s"))

    again = service.add_connection("prod", "DSN_B")

    assert again.id == first.id
    assert again.dsn_env == "DSN_B"
    assert again.limits.statement_timeout == "5s"


def test_new_connection_gets_default_limits(tmp_path: Path) -> None:
    service = AnalyzerService(home=tmp_path)

    conn = service.add_connection("prod", "DSN_A")

    assert conn.limits == SessionLimits()
