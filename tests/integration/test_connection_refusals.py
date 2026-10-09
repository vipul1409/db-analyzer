import pytest

from db_analyzer.core.model import ConnectionRefused
from db_analyzer.service import AnalyzerService

from .conftest import SUPPORTED, UNSUPPORTED, DsnEnv

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("major", SUPPORTED)
def test_superuser_login_is_refused(service: AnalyzerService, dsn_env: DsnEnv, major: int) -> None:
    conn = service.add_connection(name="admin", dsn_env=dsn_env(major, "postgres"))

    with pytest.raises(ConnectionRefused, match="role is not read-only"):
        service.probe(conn.id)


@pytest.mark.parametrize("major", SUPPORTED)
def test_writable_login_is_refused(service: AnalyzerService, dsn_env: DsnEnv, major: int) -> None:
    conn = service.add_connection(name="writer", dsn_env=dsn_env(major, "db_writer"))

    with pytest.raises(ConnectionRefused, match="role is not read-only"):
        service.probe(conn.id)


@pytest.mark.parametrize("major", SUPPORTED)
def test_login_with_write_grants_is_refused_even_when_read_only_by_default(
    service: AnalyzerService, dsn_env: DsnEnv, major: int
) -> None:
    conn = service.add_connection(name="sneaky", dsn_env=dsn_env(major, "db_sneaky"))

    with pytest.raises(ConnectionRefused, match="role is not read-only: it can write"):
        service.probe(conn.id)


@pytest.mark.parametrize("major", UNSUPPORTED)
def test_server_below_15_is_refused(service: AnalyzerService, dsn_env: DsnEnv, major: int) -> None:
    conn = service.add_connection(name="old", dsn_env=dsn_env(major, "db_analyzer"))

    with pytest.raises(ConnectionRefused, match=f"PostgreSQL {major} is not supported"):
        service.probe(conn.id)


def test_missing_dsn_variable_is_refused(service: AnalyzerService) -> None:
    conn = service.add_connection(name="nowhere", dsn_env="DBX_TEST_DSN_NOT_SET")

    with pytest.raises(ConnectionRefused, match="DBX_TEST_DSN_NOT_SET is not set"):
        service.probe(conn.id)
