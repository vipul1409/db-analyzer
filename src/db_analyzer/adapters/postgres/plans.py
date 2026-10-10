"""Generic plans of normalized workload statements (ADR 0002), normalized into PlanNode trees.

Only verbatim workload text is planned, under the internal guard profile, and nothing is
executed: PG 16+ uses EXPLAIN (GENERIC_PLAN); PG 15 prepares the statement under
force_generic_plan and EXPLAINs an EXECUTE with one NULL per placeholder, in one read-only
transaction, always deallocating it afterwards (a prepared statement outlives a rollback).

The analyzer role cannot EXPLAIN DML, so UPDATE and DELETE are planned as the SELECT that finds
their rows, and INSERT … SELECT as its SELECT.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from pglast import ast, parse_sql
from pglast.enums import A_Expr_Kind
from pglast.parser import ParseError
from pglast.stream import RawStream

from db_analyzer.adapters.postgres.queries import LIBRARY
from db_analyzer.adapters.sql_common.templates import run_template
from db_analyzer.core.model import (
    PlanNode,
    QueryRejected,
    RelationEstimate,
    StatementPlan,
    qualify,
)
from db_analyzer.safety.executor import SafeExecutor
from db_analyzer.safety.guard import children

GENERIC_PLAN_OPTION = 160000  # EXPLAIN (GENERIC_PLAN) exists from PG 16


@dataclass(frozen=True)
class Plannable:
    """The SELECT to plan for a statement, and whether it stands in for DML."""

    sql: str
    row_lookup_only: bool


def plannable(sql: str) -> Plannable | str:
    """What to plan for `sql`, or why nothing can be. An UPDATE keeps its SET values as
    `column = value` outputs, so a placeholder used only there still gets a type."""
    stmt = parse_sql(sql)[0].stmt
    if isinstance(stmt, ast.SelectStmt):
        return Plannable(sql, row_lookup_only=False)
    if isinstance(stmt, ast.UpdateStmt):
        targets = [_set_value(t) for t in stmt.targetList or ()]
        return _row_lookup(stmt, [t for t in targets if t is not None], stmt.fromClause)
    if isinstance(stmt, ast.DeleteStmt):
        return _row_lookup(stmt, [], stmt.usingClause)
    if isinstance(stmt, ast.InsertStmt):
        select = stmt.selectStmt
        if not isinstance(select, ast.SelectStmt) or select.valuesLists:
            return "INSERT … VALUES looks up no rows"
        return Plannable(_deparse(select), row_lookup_only=True)
    if isinstance(stmt, ast.MergeStmt):
        return "MERGE is not planned"
    return f"{type(stmt).__name__.removesuffix('Stmt')} is not planned"


def _set_value(target: ast.ResTarget) -> ast.ResTarget | None:
    if not isinstance(target.val, ast.Node) or isinstance(
        target.val, ast.SetToDefault | ast.MultiAssignRef
    ):
        return None
    column = ast.ColumnRef(fields=(ast.String(sval=target.name),))
    return ast.ResTarget(
        val=ast.A_Expr(
            kind=A_Expr_Kind.AEXPR_OP, name=(ast.String(sval="="),), lexpr=column, rexpr=target.val
        )
    )


def _row_lookup(
    stmt: ast.UpdateStmt | ast.DeleteStmt, targets: list[ast.ResTarget], extra: Any
) -> Plannable:
    select = ast.SelectStmt(
        targetList=tuple(targets) or (ast.ResTarget(val=ast.A_Const(val=ast.Integer(1))),),
        fromClause=(stmt.relation, *(extra or ())),
        whereClause=stmt.whereClause,
        withClause=stmt.withClause,
    )
    return Plannable(_deparse(select), row_lookup_only=True)


def _deparse(node: ast.Node) -> str:
    return str(RawStream()(node))


def generic_plan(executor: SafeExecutor, text: str, purpose: str = "plan") -> StatementPlan:
    """The generic plan of one normalized workload statement. A statement the guard refuses or
    the server cannot plan comes back with the reason instead."""
    target = plannable(text)
    if isinstance(target, str):
        return StatementPlan(None, skipped=target)
    try:
        raw = explain(executor, target.sql, purpose)
    except QueryRejected as e:
        return StatementPlan(None, target.row_lookup_only, skipped=f"refused: {e.reason}")
    except psycopg.Error as e:
        first = (str(e).strip().splitlines() or [type(e).__name__])[0]
        return StatementPlan(None, target.row_lookup_only, skipped=f"not planned: {first}")
    return StatementPlan(normalize(raw), target.row_lookup_only)


def explain(executor: SafeExecutor, sql: str, purpose: str = "plan") -> dict[str, Any]:
    """The raw EXPLAIN (VERBOSE, FORMAT JSON) root node of the generic plan of a SELECT."""
    if executor.server_version_num >= GENERIC_PLAN_OPTION:
        rows = executor.execute(f"EXPLAIN (GENERIC_PLAN, VERBOSE, FORMAT JSON) {sql}", purpose)
    else:
        name = f"dbx_plan_{uuid.uuid4().hex[:12]}"
        nulls = ", ".join(["NULL"] * _placeholders(sql))
        rows = executor.execute_sequence(
            [
                "SET LOCAL plan_cache_mode = force_generic_plan",
                f"PREPARE {name} AS {sql}",
                f"EXPLAIN (VERBOSE, FORMAT JSON) EXECUTE {name}" + (f"({nulls})" if nulls else ""),
            ],
            purpose,
            cleanup=f"DEALLOCATE {name}",
        )
    return dict(rows[0]["QUERY PLAN"][0]["Plan"])


def _placeholders(sql: str) -> int:
    return max((int(n) for n in re.findall(r"\$(\d+)", sql)), default=0)


# EXPLAIN keys for the part of a node an index or join resolves, and the part checked after.
_CONDITIONS = ("Index Cond", "Recheck Cond", "Hash Cond", "Merge Cond", "TID Cond")
_FILTERS = ("Filter", "Join Filter")


def normalize(raw: dict[str, Any]) -> PlanNode:
    """A PlanNode tree from one node of `EXPLAIN (VERBOSE, FORMAT JSON)` output. Keeps what the
    plan rules and evidence need; costs, outputs and planner flags are dropped."""
    schema = raw.get("Schema")
    relation = raw.get("Relation Name")
    filter_ = " AND ".join(str(raw[k]) for k in _FILTERS if raw.get(k)) or None
    alias = str(raw.get("Alias") or relation)
    return PlanNode(
        node_type=str(raw["Node Type"]),
        rows=int(raw["Plan Rows"]),
        width=int(raw["Plan Width"]),
        total_cost=float(raw["Total Cost"]),
        relation=qualify(schema, relation) if relation else None,
        # An index lives in its table's schema.
        index=qualify(schema, raw["Index Name"]) if raw.get("Index Name") else None,
        join_type=raw.get("Join Type"),
        strategy=raw.get("Strategy"),
        condition=next((str(raw[k]) for k in _CONDITIONS if raw.get(k)), None),
        filter=filter_,
        filter_columns=_columns(filter_, alias) if filter_ and relation else [],
        sort_key=[str(k) for k in raw.get("Sort Key", [])],
        children=[normalize(c) for c in raw.get("Plans", [])],
    )


def _columns(expression: str, alias: str) -> list[str]:
    """Columns of the scanned relation (by its alias, or unqualified) that an EXPLAIN filter
    reads, in order of first use. Empty when the deparsed filter is not valid SQL (e.g. it
    names a SubPlan)."""
    try:
        select: Any = parse_sql(f"SELECT 1 WHERE {expression}")[0].stmt
    except ParseError:
        return []
    found: list[str] = []
    for ref in _column_refs(select.whereClause) if select.whereClause else []:
        *qualifier, last = ref.fields or ()
        if not isinstance(last, ast.String):
            continue
        names = [q.sval for q in qualifier if isinstance(q, ast.String)]
        if names in ([], [alias]) and str(last.sval) not in found:
            found.append(str(last.sval))
    return found


def _column_refs(node: Any) -> list[ast.ColumnRef]:
    if isinstance(node, ast.ColumnRef):
        return [node]
    return [r for child in children(node) for r in _column_refs(child)]


def relation_estimates(executor: SafeExecutor, server_version_num: int) -> list[RelationEstimate]:
    """What the planner knows about every table, partition and materialized view: plans scan
    partitions, so each is listed on its own."""
    template = LIBRARY.get("relation_estimates", server_version_num)
    return [
        RelationEstimate(
            name=qualify(str(r["schema"]), str(r["name"])),
            estimated_rows=int(r["estimated_rows"]) if r["estimated_rows"] >= 0 else None,
            live_rows=int(r["live_rows"]),
            modified_since_analyze=int(r["modified_since_analyze"]),
            analyzed=bool(r["analyzed"]),
        )
        for r in run_template(executor, template, purpose="plan")
    ]
