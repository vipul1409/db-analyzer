"""The committed pg_catalog snapshot covers every supported server. If this fails after adding a
Postgres version or fixture, regenerate it: uv run python -m tests.fixtures.catalog_snapshot"""

import json

import psycopg
import pytest

from tests.fixtures.catalog_snapshot import QUERIES, SNAPSHOT
from tests.fixtures.dataset import fixture_dsn

from .conftest import SUPPORTED

pytestmark = pytest.mark.integration

COMMITTED = {k: set(v) for k, v in json.loads(SNAPSHOT.read_text()).items() if k in QUERIES}


@pytest.mark.parametrize("major", SUPPORTED)
@pytest.mark.parametrize("key", QUERIES)
def test_snapshot_covers_server(major: int, key: str) -> None:
    with psycopg.connect(fixture_dsn(major, "postgres", "postgres")) as conn:
        on_server = {r[0] for r in conn.execute(QUERIES[key])}

    assert on_server <= COMMITTED[key], sorted(on_server - COMMITTED[key])
