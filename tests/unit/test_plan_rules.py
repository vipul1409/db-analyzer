"""Each plan rule against the real generic plans of the seeded statements (the normalizer's
golden files): it fires on the statements the ground truth lists it for, and on no other."""

import json
from typing import Any

import pytest

from db_analyzer.analyzers import plan_rules
from db_analyzer.core.model import (
    CollectionKind,
    CollectionRef,
    PlanNode,
    RelationEstimate,
    UnindexedForeignKey,
)
from tests.fixtures.dataset import GROUND_TRUTH
from tests.fixtures.plans import DIRECTORY, name

SLOW = GROUND_TRUTH["slow_queries"]
RULES = sorted({r for q in SLOW for r in q["plan_rules"]})


def tree(d: dict[str, Any]) -> PlanNode:
    return PlanNode(**{**d, "children": [tree(c) for c in d["children"]]})


def seeded_plan(major: int, match: str) -> PlanNode:
    return tree(
        json.loads((DIRECTORY / f"pg{major}" / f"{name(match)}.normalized.json").read_text())
    )


def fk(table: str, *columns: str) -> UnindexedForeignKey:
    return UnindexedForeignKey(
        CollectionRef("public", table, CollectionKind.TABLE), f"{table}_fkey", list(columns)
    )


def fresh(table: str, rows: int) -> RelationEstimate:
    return RelationEstimate(f"public.{table}", rows, rows, 0, analyzed=True)


# The seeded database as the catalog describes it (tests/fixtures/dataset, CI scale).
SHOP = plan_rules.PlanContext(
    relations={
        r.name: r
        for r in (
            fresh("tenants", 20),
            fresh("accounts", 2_000),
            fresh("bookings", 20_000),
            fresh("booking_items", 60_000),
            fresh("events", 60_000),
            # Analyzed at 100 rows, then bulk-loaded.
            RelationEstimate("public.legacy_imports", 100, 50_000, 49_900, analyzed=True),
        )
    },
    unindexed_foreign_keys=[
        fk("bookings", "account_id"),
        fk("bookings", "tenant_id"),
        fk("booking_items", "booking_id"),
        fk("audit_log", "tenant_id"),
    ],
)


def fired(plan: PlanNode, temp_blks_written: int = 0) -> set[str]:
    return {r.rule for r in plan_rules.evaluate(plan, SHOP, temp_blks_written=temp_blks_written)}


def test_the_ground_truth_names_all_six_rules() -> None:
    assert set(RULES) == set(plan_rules.RULES)


@pytest.mark.parametrize("major", [15, 16, 17, 18])
@pytest.mark.parametrize("rule", RULES)
def test_each_rule_fires_on_its_seeded_statements_and_no_others(major: int, rule: str) -> None:
    for q in SLOW:
        temp = 1_000 if q.get("temp_blocks") else 0
        expected = rule in q["plan_rules"]

        assert (rule in fired(seeded_plan(major, q["match"]), temp)) == expected, q["match"]


def test_a_sort_that_wrote_no_temporary_files_is_not_reported_as_spilling() -> None:
    sort = seeded_plan(17, "FROM events ORDER BY k")

    assert "spill_prone_sort_or_hash" not in fired(sort, temp_blks_written=0)


def test_a_nested_loop_over_few_rows_is_not_big() -> None:
    small = PlanNode(
        "Nested Loop",
        rows=10,
        width=8,
        total_cost=20.0,
        children=[
            PlanNode("Seq Scan", rows=10, width=8, total_cost=1.0, relation="public.tenants"),
            PlanNode(
                "Index Scan",
                rows=1,
                width=8,
                total_cost=1.0,
                relation="public.accounts",
                index="public.accounts_pkey",
                condition="(id = t.id)",
            ),
        ],
    )

    assert fired(small) == set()


def test_each_reason_quotes_the_plan_node_it_is_about() -> None:
    plan = seeded_plan(17, "FROM bookings WHERE account_id = $1")

    reasons = {r.rule: r for r in plan_rules.evaluate(plan, SHOP, temp_blks_written=0)}

    seq = reasons["seq_scan_selective_filter"]
    assert seq.node == plan.line()
    assert "public.bookings" in seq.explanation and "20,000" in seq.explanation
    assert "account_id" in reasons["unindexed_fk_filter"].explanation


def test_an_index_walked_in_order_without_a_filter_is_not_non_selective() -> None:
    walk = PlanNode(
        "Limit",
        rows=20,
        width=8,
        total_cost=1.0,
        children=[
            PlanNode(
                "Index Scan",
                rows=60_000,
                width=8,
                total_cost=900.0,
                relation="public.events",
                index="public.events_pkey",
            )
        ],
    )

    assert fired(walk) == set()
