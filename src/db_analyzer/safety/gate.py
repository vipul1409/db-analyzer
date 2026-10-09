"""EXPLAIN gate: refuse a statement whose plan is too expensive before it runs.

Reads the output of `EXPLAIN (FORMAT JSON, COSTS ON)`: total cost and result rows of the root
node, and the largest row estimate of any scan node anywhere in the tree.
"""

from collections.abc import Iterator
from typing import Any

from db_analyzer.core.model import GateLimits, GateRejected, PlanMetrics


def check(explain_json: list[dict[str, Any]], limits: GateLimits) -> PlanMetrics:
    root = explain_json[0]["Plan"]
    plan = PlanMetrics(
        total_cost=float(root["Total Cost"]),
        result_rows=int(root["Plan Rows"]),
        scan_rows=max((int(n["Plan Rows"]) for n in _nodes(root) if _is_scan(n)), default=0),
    )
    if plan.total_cost > limits.max_total_cost:
        raise GateRejected("total_cost", plan.total_cost, limits.max_total_cost)
    if plan.result_rows > limits.max_result_rows:
        raise GateRejected("result_rows", plan.result_rows, limits.max_result_rows)
    if plan.scan_rows > limits.max_scan_rows:
        raise GateRejected("scan_rows", plan.scan_rows, limits.max_scan_rows)
    return plan


def _nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _is_scan(node: dict[str, Any]) -> bool:
    # Seq/Index/Index Only/Bitmap Heap/Bitmap Index/Tid/Sample/... Scan, but not Function Scan
    # (set-returning functions) or CTE/Subquery Scan, which re-read rows counted elsewhere.
    kind = str(node["Node Type"])
    return kind.endswith("Scan") and kind not in _NOT_RELATION_SCANS


_NOT_RELATION_SCANS = {
    "Function Scan",
    "CTE Scan",
    "Subquery Scan",
    "Values Scan",
    "WorkTable Scan",
    "Named Tuplestore Scan",
    "Result Scan",
    "Table Function Scan",
}
