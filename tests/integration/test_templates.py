"""Every catalog template runs on every supported version and returns exactly its declared
columns, as the read-only analyzer role, through SafeExecutor (guard and EXPLAIN gate)."""

import pytest

from db_analyzer.adapters.postgres.queries import LIBRARY
from db_analyzer.adapters.postgres.session import open_session
from db_analyzer.adapters.sql_common.templates import Template
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
