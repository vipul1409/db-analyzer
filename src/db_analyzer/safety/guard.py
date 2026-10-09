"""QueryGuard: parse with the real Postgres parser and allow only read-only statements.

Minimal allowlist: one top-level SELECT. Anywhere in the tree (subqueries, set operations,
CTEs) there may be no SELECT INTO, no locking clause and no data-modifying CTE. The full
allowlist (functions, EXPLAIN, guard profiles) follows in the safety ticket.
"""

from typing import Any

from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.visitors import Visitor

from db_analyzer.core.model import QueryRejected


def check(sql: str) -> None:
    try:
        stmts = parse_sql(sql)
    except ParseError as e:
        raise QueryRejected(f"unparseable: {e}") from e
    if len(stmts) != 1:
        raise QueryRejected(f"expected exactly one statement, got {len(stmts)}")
    stmt = stmts[0].stmt
    if not isinstance(stmt, ast.SelectStmt):
        raise QueryRejected(f"{type(stmt).__name__} is not allowed")
    _ReadOnlyChecker()(stmt)


class _ReadOnlyChecker(Visitor):
    def visit_SelectStmt(self, ancestors: Any, node: ast.SelectStmt) -> None:
        if node.intoClause is not None:
            raise QueryRejected("SELECT INTO is not allowed")
        if node.lockingClause:
            raise QueryRejected("locking clauses (FOR UPDATE/SHARE) are not allowed")

    def visit_CommonTableExpr(self, ancestors: Any, node: ast.CommonTableExpr) -> None:
        if not isinstance(node.ctequery, ast.SelectStmt):
            raise QueryRejected("data-modifying CTEs are not allowed")
