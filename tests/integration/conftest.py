import os
from collections.abc import Callable
from pathlib import Path

import pytest

from db_analyzer.service import AnalyzerService

SUPPORTED = [int(v) for v in os.environ.get("DBX_TEST_PG_VERSIONS", "15,16,17,18").split(",") if v]
UNSUPPORTED = [
    int(v) for v in os.environ.get("DBX_TEST_UNSUPPORTED_PG_VERSIONS", "14").split(",") if v
]


def dsn(major: int, role: str = "db_analyzer") -> str:
    """Fixture roles use their name as password: db_analyzer, db_writer, postgres."""
    return f"postgresql://{role}:{role}@localhost:{5400 + major}/app"


@pytest.fixture
def service(tmp_path: Path) -> AnalyzerService:
    return AnalyzerService(home=tmp_path)


DsnEnv = Callable[[int, str], str]


@pytest.fixture
def dsn_env(monkeypatch: pytest.MonkeyPatch) -> DsnEnv:
    """Put a fixture DSN in an environment variable and return the variable's name."""

    def put(major: int, role: str = "db_analyzer") -> str:
        name = f"DBX_TEST_DSN_{major}_{role.upper()}"
        monkeypatch.setenv(name, dsn(major, role))
        return name

    return put
