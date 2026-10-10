"""Golden files: the normalized tree of every seeded slow statement's real generic plan, on the
PG 15 PREPARE path and the PG 16+ GENERIC_PLAN path. Recapture the raw plans with
`uv run python -m tests.fixtures.plans --pg 15 --pg 16 --pg 17 --pg 18` and rewrite the golden
files with DBX_UPDATE_GOLDEN=1."""

import dataclasses
import json
import os

import pytest

from db_analyzer.adapters.postgres import plans
from tests.fixtures.plans import DIRECTORY, raw_plans

CASES = [(major, name) for major in (15, 16, 17, 18) for name in raw_plans(major)]


@pytest.mark.parametrize(("major", "name"), CASES, ids=[f"pg{m}-{n}" for m, n in CASES])
def test_normalized_plan_matches_its_golden_file(major: int, name: str) -> None:
    normalized = dataclasses.asdict(plans.normalize(raw_plans(major)[name]))
    golden = DIRECTORY / f"pg{major}" / f"{name}.normalized.json"
    if os.environ.get("DBX_UPDATE_GOLDEN"):
        golden.write_text(json.dumps(normalized, indent=1) + "\n")

    assert normalized == json.loads(golden.read_text())


def test_every_seeded_statement_has_a_golden_plan_on_both_paths() -> None:
    assert {len(raw_plans(m)) for m in (15, 16, 17, 18)} == {7}
