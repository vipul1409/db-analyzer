"""QueryGuard: parse with the real Postgres parser and accept only read-only statements.

One statement only. The profile is chosen by the code path, never by the SQL's author:

- agent: SQL the LLM wrote. SELECT, SHOW, and EXPLAIN (without ANALYZE) of a SELECT.
- internal: vetted templates and verbatim workload text. Also the generic-plan path:
  PREPARE of a SELECT, EXPLAIN EXECUTE, DEALLOCATE and SET LOCAL plan_cache_mode.

EXPLAIN and PREPARE never wrap DML, in any profile (ADR 0002). Anywhere in a SELECT's tree
there may be no SELECT INTO, no locking clause, no data-modifying CTE, no function outside the
allowlist (safety/functions.py) and no operator from a user schema.

The guard also decides, from the parse tree, whether the EXPLAIN gate applies: statements that
execute nothing, and SELECTs that read only catalog relations, are exempt.

Known limit: names are checked, not resolved. A function or operator planted in a user schema
under a pg_catalog name, or behind a view or cast, is not visible to the parser. The read-only
transaction and role still refuse any write it attempts.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from pglast import ast, parse_sql
from pglast.enums import VariableSetKind
from pglast.parser import ParseError
from pglast.visitors import Visitor

from db_analyzer.core.model import QueryRejected
from db_analyzer.safety import functions as fn

Profile = Literal["agent", "internal"]
Kind = Literal["select", "explain", "show", "set_local", "prepare", "deallocate"]

SETTABLE_LOCALLY = frozenset({"plan_cache_mode"})


@dataclass(frozen=True)
class Checked:
    kind: Kind
    gated: bool  # whether the EXPLAIN gate must check it before it runs


def check(sql: str, profile: Profile) -> Checked:
    try:
        stmts = parse_sql(sql)
    except ParseError as e:
        raise QueryRejected(f"unparseable: {e}") from e
    if len(stmts) != 1:
        raise QueryRejected(f"expected exactly one statement, got {len(stmts)}")
    stmt = stmts[0].stmt

    if isinstance(stmt, ast.SelectStmt):
        return Checked("select", gated=not _check_select(stmt).catalog_only)
    if isinstance(stmt, ast.ExplainStmt):
        _check_explain(stmt, profile)
        return Checked("explain", gated=False)
    if isinstance(stmt, ast.VariableShowStmt):
        return Checked("show", gated=False)
    if isinstance(stmt, ast.VariableSetStmt):
        _check_set(stmt, profile)
        return Checked("set_local", gated=False)
    if isinstance(stmt, ast.PrepareStmt):
        if not isinstance(stmt.query, ast.SelectStmt):
            raise QueryRejected("PREPARE may only wrap a SELECT")
        _check_select(stmt.query)
        _internal_only("PREPARE", profile)
        return Checked("prepare", gated=False)
    if isinstance(stmt, ast.DeallocateStmt):
        _internal_only("DEALLOCATE", profile)
        return Checked("deallocate", gated=False)
    raise QueryRejected(f"{_statement_name(stmt)} is not allowed")


def _check_explain(stmt: ast.ExplainStmt, profile: Profile) -> None:
    for option in stmt.options or ():
        if option.defname == "analyze":
            raise QueryRejected("EXPLAIN ANALYZE is not allowed: it executes the statement")
    if isinstance(stmt.query, ast.SelectStmt):
        _check_select(stmt.query)
    elif isinstance(stmt.query, ast.ExecuteStmt):
        _internal_only("EXPLAIN EXECUTE", profile)
    else:
        raise QueryRejected(f"EXPLAIN may only wrap a SELECT, not {_statement_name(stmt.query)}")


def _check_set(stmt: ast.VariableSetStmt, profile: Profile) -> None:
    if not (
        stmt.is_local
        and stmt.kind == VariableSetKind.VAR_SET_VALUE
        and stmt.name in SETTABLE_LOCALLY
    ):
        raise QueryRejected(
            "SET is not allowed; only SET LOCAL of " + ", ".join(sorted(SETTABLE_LOCALLY))
        )
    _internal_only("SET LOCAL", profile)


def _internal_only(what: str, profile: Profile) -> None:
    if profile != "internal":
        raise QueryRejected(f"{what} is not allowed in the agent profile")


def _statement_name(stmt: Any) -> str:
    return type(stmt).__name__.removesuffix("Stmt")


@dataclass
class _SelectFacts:
    catalog_only: bool


def _check_select(stmt: ast.SelectStmt) -> _SelectFacts:
    checker = _SelectChecker()
    checker(stmt)
    catalog_only = (
        not checker.recursive
        and all(_is_catalog_relation(r) for r in _relations(stmt, frozenset()))
        and checker.row_sources <= fn.CATALOG_ROW_SOURCES
        and not (checker.functions & fn.READS_RELATION_DATA)
        and checker.functions & fn.SET_RETURNING <= fn.CATALOG_ROW_SOURCES
    )
    return _SelectFacts(catalog_only=catalog_only)


def _relations(node: Any, ctes: frozenset[str]) -> Iterator[ast.RangeVar]:
    """Every relation the statement reads. An unqualified name is a CTE, not a relation, only
    where that CTE is in scope: inside its WITH's query, and for a non-recursive WITH only in
    the CTEs that follow it."""
    if isinstance(node, ast.RangeVar):
        if not (node.schemaname is None and node.relname in ctes):
            yield node
        return
    if isinstance(node, ast.SelectStmt) and node.withClause is not None:
        with_ = node.withClause
        names = [c.ctename for c in with_.ctes or () if c.ctename]
        for i, cte in enumerate(with_.ctes or ()):
            yield from _relations(cte.ctequery, ctes | set(names if with_.recursive else names[:i]))
        for child in _children(node, skip="withClause"):
            yield from _relations(child, ctes | set(names))
        return
    for child in _children(node):
        yield from _relations(child, ctes)


def _is_catalog_relation(r: ast.RangeVar) -> bool:
    if r.relname in fn.EXTENSION_RELATIONS:
        return True
    if r.schemaname is None:
        return r.relname in fn.CATALOG_RELATIONS
    return r.schemaname in fn.CATALOG_SCHEMAS


def _children(node: ast.Node, skip: str | None = None) -> Iterator[ast.Node]:
    slots: dict[str, Any] = type(node).__slots__  # type: ignore[attr-defined]
    for slot in slots:
        if slot != skip:
            yield from _nodes(getattr(node, slot))


def _nodes(value: Any) -> Iterator[ast.Node]:
    """The AST nodes in a slot value: a node, or nodes nested in (lists of) lists."""
    if isinstance(value, ast.Node):
        yield value
    elif isinstance(value, tuple | list):
        for item in value:
            yield from _nodes(item)


class _SelectChecker(Visitor):
    """Walks a whole SELECT tree: subqueries, set operations, CTEs, FROM, WHERE, everywhere."""

    def __init__(self) -> None:
        super().__init__()
        self.recursive = False
        self.functions: set[str] = set()
        self.row_sources: set[str] = set()

    def visit_SelectStmt(self, ancestors: Any, node: ast.SelectStmt) -> None:
        if node.intoClause is not None:
            raise QueryRejected("SELECT INTO is not allowed")
        if node.lockingClause:
            raise QueryRejected("locking clauses (FOR UPDATE/SHARE) are not allowed")
        if node.withClause is not None and node.withClause.recursive:
            self.recursive = True

    def visit_CommonTableExpr(self, ancestors: Any, node: ast.CommonTableExpr) -> None:
        if not isinstance(node.ctequery, ast.SelectStmt):
            raise QueryRejected("data-modifying CTEs are not allowed")

    def visit_RangeFunction(self, ancestors: Any, node: ast.RangeFunction) -> None:
        for call, _coldefs in node.functions or ():
            if isinstance(call, ast.FuncCall) and call.funcname:
                self.row_sources.add(call.funcname[-1].sval)
            else:
                self.row_sources.add("?")  # e.g. ROWS FROM over an expression: not exempt

    def visit_FuncCall(self, ancestors: Any, node: ast.FuncCall) -> None:
        *schema, name = (s.sval for s in node.funcname or ())
        qualified = ".".join([*schema, name])
        allowed = name in fn.EXTENSION_FUNCTIONS or (
            schema in ([], ["pg_catalog"]) and name in fn.PG_CATALOG_FUNCTIONS
        )
        if not allowed:
            raise QueryRejected(f"function {qualified} is not allowed")
        self.functions.add(name)

    def visit_A_Expr(self, ancestors: Any, node: ast.A_Expr) -> None:
        *schema, name = (s.sval for s in node.name or ())
        if schema and schema != ["pg_catalog"]:
            raise QueryRejected(f"operator {'.'.join([*schema, name])} is not allowed")
