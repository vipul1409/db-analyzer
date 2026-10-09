"""Regenerate src/db_analyzer/safety/pg_catalog.json from the Docker fixtures (PG 15-18).

    uv run python -m tests.fixtures.catalog_snapshot

The snapshot lists, across all supported versions, the stable and immutable pg_catalog function
names, the set-returning ones, and the pg_catalog relation names. db_analyzer.safety.functions
builds the guard's allowlists from it; tests/integration/test_catalog_snapshot.py fails when it
is out of date.
"""

import json
from pathlib import Path

import psycopg

from tests.fixtures.dataset import fixture_dsn

SNAPSHOT = Path(__file__).parents[2] / "src" / "db_analyzer" / "safety" / "pg_catalog.json"
VERSIONS = (15, 16, 17, 18)

FUNCTIONS = """
SELECT DISTINCT proname FROM pg_proc
WHERE pronamespace = 'pg_catalog'::regnamespace AND prokind IN ('f', 'a', 'w')
  AND provolatile IN ('i', 's')
"""
SET_RETURNING = """
SELECT DISTINCT proname FROM pg_proc
WHERE pronamespace = 'pg_catalog'::regnamespace AND proretset
"""
RELATIONS = """
SELECT relname FROM pg_class
WHERE relnamespace = 'pg_catalog'::regnamespace AND relkind IN ('r', 'v', 'm', 'p')
"""


QUERIES = {
    "stable_functions": FUNCTIONS,
    "set_returning_functions": SET_RETURNING,
    "relations": RELATIONS,
}


def read(major: int) -> dict[str, set[str]]:
    with psycopg.connect(fixture_dsn(major, "postgres", "postgres")) as conn:
        return {key: {r[0] for r in conn.execute(sql)} for key, sql in QUERIES.items()}


def main() -> None:
    snapshot: dict[str, set[str]] = {key: set() for key in QUERIES}
    for major in VERSIONS:
        for key, names in read(major).items():
            snapshot[key] |= names
    SNAPSHOT.write_text(
        json.dumps(
            {"versions": list(VERSIONS)} | {k: sorted(v) for k, v in snapshot.items()},
            indent=0,
        )
        + "\n"
    )
    print({k: len(v) for k, v in snapshot.items()}, "->", SNAPSHOT)


if __name__ == "__main__":
    main()
