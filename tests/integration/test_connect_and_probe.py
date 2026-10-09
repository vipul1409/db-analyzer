import pytest

from db_analyzer.service import AnalyzerService

from .conftest import SUPPORTED, DsnEnv

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("major", SUPPORTED)
def test_probe_reports_version_and_primary(
    service: AnalyzerService, dsn_env: DsnEnv, major: int
) -> None:
    conn = service.add_connection(name=f"pg{major}", dsn_env=dsn_env(major, "db_analyzer"))

    probe = service.probe(conn.id)

    assert probe.server_version_num // 10000 == major
    assert probe.in_recovery is False
    assert probe.host_type == "self_managed"
