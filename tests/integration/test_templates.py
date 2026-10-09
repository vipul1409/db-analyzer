"""Every catalog template runs on every supported version and returns exactly its declared
columns, as the read-only analyzer role, through SafeExecutor (guard and EXPLAIN gate)."""

from pathlib import Path

import pytest

from db_analyzer.adapters.postgres.queries import LIBRARY
from db_analyzer.adapters.postgres.session import open_session
from db_analyzer.adapters.sql_common.templates import Template, TemplateLibrary, run_template
from db_analyzer.core.model import SessionLimits
from db_analyzer.safety.executor import SafeExecutor

from .conftest import SUPPORTED, seeded_dsn

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("major", SUPPORTED)
@pytest.mark.parametrize("template", LIBRARY.all(), ids=lambda t: t.path.name)
def test_template_returns_declared_columns(major: int, template: Template) -> None:
    if template.min_version > major:
        pytest.skip(f"needs PostgreSQL {template.min_version}")
    with open_session(seeded_dsn(major), SessionLimits()) as conn:
        rows = SafeExecutor(conn, "c1", audit=lambda _: None).execute(template.sql, "test")

    assert rows
    assert tuple(rows[0]) == template.columns


def test_template_results_drop_undeclared_columns(tmp_path: Path) -> None:
    (tmp_path / "accounts.sql").write_text(
        "-- name: accounts\n-- columns: n\n-- min_version: 15\n-- privilege: SELECT\n"
        "SELECT count(*) AS n, min(email) AS leaked FROM accounts\n"
    )
    template = TemplateLibrary(tmp_path).get("accounts", 150000)

    with open_session(seeded_dsn(SUPPORTED[0]), SessionLimits()) as conn:
        rows = run_template(SafeExecutor(conn, "c1", audit=lambda _: None), template, "test")

    assert rows == [{"n": 2_000}]
