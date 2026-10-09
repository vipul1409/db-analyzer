"""Spike (#4): can normalized pg_stat_statements text be planned, read-only, as db_analyzer?

Throwaway. Needs: make db-up PG="pg15 pg16 pg15-standby pg16-standby" && make db-seed PG="15 16"
Run:           uv run pytest -m spike -s tests/spikes
Results are recorded in docs/adr/0002-generic-plans-for-normalized-statements.md.
"""

import re
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from tests.fixtures.dataset import GROUND_TRUTH, fixture_dsn

pytestmark = pytest.mark.spike

TARGETS = {  # name -> (major, port)
    "pg15-primary": (15, 5415),
    "pg15-standby": (15, 5515),
    "pg16-primary": (16, 5416),
    "pg16-standby": (16, 5516),
}
Planner = Callable[[psycopg.Connection[Any], str], dict[str, Any]]


def normalized_statements(major: int) -> dict[str, str]:
    """Ground-truth slow statements as pg_stat_statements stored them on the primary."""
    with psycopg.connect(fixture_dsn(major, "postgres", "shop")) as conn:
        found = {}
        for q in GROUND_TRUTH["slow_queries"]:
            row = conn.execute(
                # Exclude our own PREPARE/EXPLAIN wrappers: with track=all they are recorded too.
                """SELECT query FROM pg_stat_statements
                   WHERE query LIKE '%%' || %s || '%%'
                     AND userid <> 'db_analyzer'::regrole
                   ORDER BY calls DESC LIMIT 1""",
                (q["match"],),
            ).fetchone()
            assert row, q["match"]
            found[q["match"]] = row[0]
        return found


def param_count(query: str) -> int:
    return max((int(n) for n in re.findall(r"\$(\d+)", query)), default=0)


def plan_via_prepare(conn: psycopg.Connection[Any], query: str) -> dict[str, Any]:
    """PG 15 path: PREPARE under force_generic_plan, EXPLAIN EXECUTE with NULLs, DEALLOCATE."""
    nulls = ", ".join(["NULL"] * param_count(query))
    explain = (
        f"EXPLAIN (FORMAT JSON) EXECUTE dbx_q({nulls})"
        if nulls
        else ("EXPLAIN (FORMAT JSON) EXECUTE dbx_q")
    )
    conn.execute(b"DEALLOCATE ALL")  # a statement prepared before an error survives rollback
    with conn.transaction(force_rollback=True):
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        conn.execute(f"PREPARE dbx_q AS {query}".encode())
        row = conn.execute(explain.encode()).fetchone()
        conn.execute(b"DEALLOCATE dbx_q")
    assert row is not None
    return dict(row[0][0]["Plan"])


def plan_via_generic_plan_option(conn: psycopg.Connection[Any], query: str) -> dict[str, Any]:
    """PG 16+ path: EXPLAIN (GENERIC_PLAN)."""
    with conn.transaction(force_rollback=True):
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute(f"EXPLAIN (GENERIC_PLAN, FORMAT JSON) {query}".encode()).fetchone()
    assert row is not None
    return dict(row[0][0]["Plan"])


def node_types(plan: dict[str, Any]) -> list[str]:
    return [plan["Node Type"], *(t for c in plan.get("Plans", []) for t in node_types(c))]


def attempt(planner: Planner, conn: psycopg.Connection[Any], query: str) -> str:
    try:
        return " > ".join(node_types(planner(conn, query)))
    except psycopg.Error as e:
        return f"ERROR {type(e).__name__}: {str(e).strip().splitlines()[0]}"


@pytest.mark.parametrize("target", TARGETS)
def test_generic_plans(target: str) -> None:
    major, port = TARGETS[target]
    statements = normalized_statements(major)
    dsn = fixture_dsn(major, "db_analyzer", "shop").replace(f":{5400 + major}/", f":{port}/")
    paths: dict[str, Planner] = {"prepare": plan_via_prepare}
    if major >= 16:
        paths["generic_plan"] = plan_via_generic_plan_option
    print(f"\n== {target} as db_analyzer")
    with psycopg.connect(dsn, autocommit=True) as conn:
        in_recovery = conn.execute("SELECT pg_is_in_recovery()").fetchone()
        assert in_recovery is not None and in_recovery[0] == target.endswith("standby")
        for match, query in statements.items():
            for path, planner in paths.items():
                print(f"  [{path:12}] {match[:45]:45} -> {attempt(planner, conn, query)}")


def dml_as_select(query: str) -> str:
    """Fallback for DML: plan only the row-finding part. UPDATE/DELETE ... WHERE c becomes
    SELECT 1 FROM <target> [, <FROM/USING items>] WHERE c (same $n parameters)."""
    from pglast import ast, parse_sql
    from pglast.stream import RawStream

    stmt: Any = parse_sql(query)[0].stmt
    extra = stmt.fromClause if isinstance(stmt, ast.UpdateStmt) else stmt.usingClause
    select = ast.SelectStmt(
        targetList=(ast.ResTarget(val=ast.A_Const(val=ast.Integer(1))),),
        fromClause=(stmt.relation, *(extra or ())),
        whereClause=stmt.whereClause,
    )
    return str(RawStream()(select))


def test_dml_cause_and_select_fallback() -> None:
    update = normalized_statements(15)["UPDATE bookings SET amount = amount WHERE tenant_id = $1"]
    print("\n== DML: cause and fallback")
    for target, port in (("pg15-primary", 5415), ("pg15-standby", 5515)):
        superuser = fixture_dsn(15, "postgres", "shop").replace(":5415/", f":{port}/")
        analyzer = fixture_dsn(15, "db_analyzer", "shop").replace(":5415/", f":{port}/")
        with psycopg.connect(superuser, autocommit=True) as conn:
            print(
                f"  {target} superuser, read-only txn, UPDATE -> "
                f"{attempt(plan_via_prepare, conn, update)}"
            )
        with psycopg.connect(analyzer, autocommit=True) as conn:
            rewritten = dml_as_select(update)
            print(f"  {target} db_analyzer, rewritten: {rewritten}")
            print(f"      -> {attempt(plan_via_prepare, conn, rewritten)}")
