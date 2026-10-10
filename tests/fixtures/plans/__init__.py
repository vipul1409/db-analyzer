"""Raw generic plans of the seeded slow statements, for the normalizer's golden-file tests.

    uv run python -m tests.fixtures.plans --pg 15 --pg 16 --pg 17 --pg 18

Captures `EXPLAIN (VERBOSE, FORMAT JSON)` of every ground-truth slow statement through the real
planning path (PREPARE on PG 15, GENERIC_PLAN on 16+) into pg<major>/<name>.json. The golden
files next to them (<name>.normalized.json) are rewritten by `DBX_UPDATE_GOLDEN=1 uv run pytest
tests/unit/test_plan_normalizer.py`; review the diff before committing.
"""

import json
import re
from pathlib import Path
from typing import Any

import psycopg

from tests.fixtures.dataset import DATABASE, GROUND_TRUTH, fixture_dsn

DIRECTORY = Path(__file__).parent


def name(match: str) -> str:
    """File name of a ground-truth slow statement, from its match text."""
    return re.sub(r"[^a-z0-9]+", "_", match.lower()).strip("_")[:48]


def normalized_statements(major: int) -> dict[str, str]:
    """Ground-truth slow statements as pg_stat_statements stored them, by match text."""
    with psycopg.connect(fixture_dsn(major, "postgres", DATABASE)) as conn:
        found = {}
        for q in GROUND_TRUTH["slow_queries"]:
            row = conn.execute(
                """SELECT query FROM pg_stat_statements
                   WHERE query LIKE '%%' || %s || '%%' AND query NOT LIKE 'EXPLAIN%%'
                     AND userid <> 'db_analyzer'::regrole
                   ORDER BY calls DESC LIMIT 1""",
                (q["match"],),
            ).fetchone()
            assert row, q["match"]
            found[q["match"]] = str(row[0])
        return found


def raw_plans(major: int) -> dict[str, dict[str, Any]]:
    """Raw plan of each seeded slow statement under its file name, per fixture version."""
    return {
        p.stem: json.loads(p.read_text())
        for p in sorted((DIRECTORY / f"pg{major}").glob("*.json"))
        if not p.stem.endswith(".normalized")
    }
