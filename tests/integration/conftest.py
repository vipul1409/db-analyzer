import os
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

from db_analyzer.core.model import Connection
from db_analyzer.service import AnalyzerService
from tests.fixtures.dataset import GROUND_TRUTH, fixture_dsn, seed

# Narrowed to PG 17 during development; restore "15,16,17,18" and "14" before release.
SUPPORTED = [int(v) for v in os.environ.get("DBX_TEST_PG_VERSIONS", "17").split(",") if v]
UNSUPPORTED = [
    int(v) for v in os.environ.get("DBX_TEST_UNSUPPORTED_PG_VERSIONS", "").split(",") if v
]


# Fixture roles: db_analyzer (read-only), db_writer, db_sneaky (both must be refused), postgres.
dsn = fixture_dsn


_seeded: set[int] = set()


def seeded_dsn(major: int, role: str = "db_analyzer") -> str:
    """DSN of the synthetic 'shop' database, seeded at CI scale once per test session."""
    if major not in _seeded:
        seed(dsn(major, "postgres", "postgres"), scale="ci")
        _seeded.add(major)
    return dsn(major, role, GROUND_TRUTH["database"])


@pytest.fixture
def service(tmp_path: Path) -> AnalyzerService:
    return AnalyzerService(home=tmp_path)


class Shop(NamedTuple):
    connection: Connection
    major: int

    @property
    def id(self) -> str:
        return self.connection.id


@pytest.fixture(params=SUPPORTED)
def shop(
    request: pytest.FixtureRequest, service: AnalyzerService, monkeypatch: pytest.MonkeyPatch
) -> Shop:
    """A Connection to the seeded 'shop' database, once per supported version."""
    major = request.param
    monkeypatch.setenv("DBX_TEST_SHOP_DSN", seeded_dsn(major))
    return Shop(service.add_connection(f"shop{major}", dsn_env="DBX_TEST_SHOP_DSN"), major)


DsnEnv = Callable[[int, str], str]


@pytest.fixture
def dsn_env(monkeypatch: pytest.MonkeyPatch) -> DsnEnv:
    """Put a fixture DSN in an environment variable and return the variable's name."""

    def put(major: int, role: str = "db_analyzer") -> str:
        name = f"DBX_TEST_DSN_{major}_{role.upper()}"
        monkeypatch.setenv(name, dsn(major, role))
        return name

    return put
