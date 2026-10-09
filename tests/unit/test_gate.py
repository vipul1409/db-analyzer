from typing import Any

import pytest

from db_analyzer.core.model import GateLimits, GateRejected
from db_analyzer.safety import gate


def plan(total_cost: float, rows: int, *children: dict[str, Any]) -> list[dict[str, Any]]:
    """EXPLAIN (FORMAT JSON) output: a one-element list wrapping the root plan node."""
    return [{"Plan": node("Aggregate", total_cost, rows, *children)}]


def node(kind: str, total_cost: float, rows: int, *children: dict[str, Any]) -> dict[str, Any]:
    n: dict[str, Any] = {"Node Type": kind, "Total Cost": total_cost, "Plan Rows": rows}
    if children:
        n["Plans"] = list(children)
    return n


LIMITS = GateLimits(max_total_cost=1_000, max_result_rows=100, max_scan_rows=10_000)


def test_plan_within_limits_passes() -> None:
    gate.check(plan(900, 1, node("Seq Scan", 800, 9_000)), LIMITS)


def test_total_cost_over_limit_is_rejected_with_structured_reason() -> None:
    with pytest.raises(GateRejected) as e:
        gate.check(plan(8_400, 1, node("Seq Scan", 800, 10)), LIMITS)

    assert (e.value.metric, e.value.value, e.value.limit) == ("total_cost", 8_400, 1_000)
    assert e.value.reason == "total cost 8.4e+03 > 1e+03"


def test_result_rows_over_limit_is_rejected() -> None:
    with pytest.raises(GateRejected) as e:
        gate.check(plan(10, 500), LIMITS)

    assert e.value.metric == "result_rows"


def test_largest_scan_anywhere_in_the_tree_counts() -> None:
    nested = node(
        "Hash Join",
        500,
        10,
        node("Seq Scan", 100, 5),
        node("Hash", 300, 20_000, node("Index Scan", 300, 20_000)),
    )

    with pytest.raises(GateRejected) as e:
        gate.check(plan(600, 1, nested), LIMITS)

    assert (e.value.metric, e.value.value) == ("scan_rows", 20_000)


def test_non_scan_nodes_do_not_count_as_scans() -> None:
    gate.check(plan(600, 1, node("Hash", 300, 50_000, node("Seq Scan", 100, 50))), LIMITS)
