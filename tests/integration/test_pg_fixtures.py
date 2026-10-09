"""Placeholder integration test: every Postgres fixture is reachable and has the extensions."""

import os

import psycopg
import pytest

pytestmark = pytest.mark.integration

REQUIRED_EXTENSIONS = {"pg_stat_statements", "hypopg", "pgstattuple"}


def _versions() -> list[int]:
    raw = os.environ.get("DBX_TEST_PG_VERSIONS", "15,16,17,18")
    return [int(v) for v in raw.split(",") if v.strip()]


def _dsn(major: int) -> str:
    return f"postgresql://postgres:postgres@localhost:{5400 + major}/app"


@pytest.mark.parametrize("major", _versions())
def test_fixture_runs_expected_major_version(major: int) -> None:
    with psycopg.connect(_dsn(major), connect_timeout=5) as conn:
        row = conn.execute("SELECT current_setting('server_version_num')::int").fetchone()
    assert row is not None
    assert row[0] // 10000 == major


@pytest.mark.parametrize("major", _versions())
def test_fixture_has_required_extensions_installable(major: int) -> None:
    with psycopg.connect(_dsn(major), connect_timeout=5) as conn:
        rows = conn.execute("SELECT name FROM pg_available_extensions").fetchall()
        preload = conn.execute("SHOW shared_preload_libraries").fetchone()
    assert {r[0] for r in rows} >= REQUIRED_EXTENSIONS
    assert preload is not None
    assert "pg_stat_statements" in preload[0]


@pytest.mark.parametrize("major", _versions())
def test_fixture_extensions_can_be_created(major: int) -> None:
    with psycopg.connect(_dsn(major), connect_timeout=5) as conn:
        for ext in sorted(REQUIRED_EXTENSIONS):
            conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        conn.execute("SELECT * FROM pg_stat_statements LIMIT 1")
        conn.rollback()
