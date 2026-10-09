"""Suite (b): with the guard bypassed, every DATABASE-layer corpus statement still fails under
the real read-only role, inside the same read-only, always-rolled-back transaction SafeExecutor
uses. GUARD-layer statements are not run here: they succeed under any read-only role (see
corpus.py), so only the guard stands in their way."""

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from db_analyzer.adapters.postgres.session import open_session
from db_analyzer.core.model import SessionLimits
from tests.integration.conftest import SUPPORTED, seeded_dsn

from .corpus import CORPUS, Forbidden

pytestmark = pytest.mark.integration

DATABASE_LAYER = [f for f in CORPUS if f.layer == "database"]

# Refused for the right reason: not "the object doesn't exist", which would pass by accident.
REFUSALS = {
    "25006": "read-only transaction",
    "42501": "insufficient privilege",
    "25001": "not allowed inside a transaction block",
    "0A000": "data-modifying WITH below the top level",
    "42601": "syntax error",
}


@pytest.fixture(params=SUPPORTED)
def session(request: pytest.FixtureRequest) -> Iterator[psycopg.Connection[Any]]:
    """A fresh session per statement: prepared statements and session state outlive the
    rolled-back transaction, and must not make a later statement fail for the wrong reason."""
    with open_session(seeded_dsn(request.param), SessionLimits(statement_timeout="5s")) as conn:
        yield conn


@pytest.mark.parametrize("forbidden", DATABASE_LAYER, ids=lambda f: f.sql[:60])
def test_database_refuses_without_the_guard(
    session: psycopg.Connection[Any], forbidden: Forbidden
) -> None:
    with pytest.raises(psycopg.Error) as e:  # noqa: SIM117 - the rollback must wrap it
        with session.transaction(force_rollback=True):
            session.execute("SET TRANSACTION READ ONLY")
            session.execute(forbidden.sql.encode())

    assert e.value.sqlstate in REFUSALS, f"{e.value.sqlstate}: {e.value}"
