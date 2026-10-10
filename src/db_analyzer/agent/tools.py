"""Tool factory: one agent tool per capability the Connection has.

Tools are thin: they call a TurnBackend (bound by the service to one Connection, Thread and
turn) and return compact, pre-digested JSON (proposal §6.2), never raw catalog rows.
"""

import json
from collections.abc import Callable
from typing import Any, Protocol

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from db_analyzer.core.model import Capability


class TurnBackend(Protocol):
    def probe(self) -> dict[str, Any]: ...

    def storage(self, top_n: int) -> dict[str, Any]: ...

    def count_exact(self, tables: list[str], all_tables: bool) -> dict[str, Any]: ...

    def sql(self, sql: str, purpose: str) -> dict[str, Any]: ...

    def top_queries(self, top_n: int) -> dict[str, Any]: ...

    def query_details(self, rank: int) -> dict[str, Any]: ...


class NoArgs(BaseModel):
    pass


class StorageArgs(BaseModel):
    top_n: int = Field(ge=1, le=50, description="How many collections to list, largest first")


class CountArgs(BaseModel):
    tables: list[str] = Field(
        max_length=50,
        description="Tables to count, schema-qualified as get_storage_stats names them",
    )
    all_tables: bool = Field(
        description="Count every table instead of `tables` (only when the user asks for all)"
    )


class TopQueriesArgs(BaseModel):
    top_n: int = Field(ge=1, le=25, description="How many statements to list, costliest first")


class QueryArgs(BaseModel):
    rank: int = Field(ge=1, description="The statement's rank in the latest get_top_queries")


class SqlArgs(BaseModel):
    sql: str = Field(description="One read-only SELECT")
    purpose: str = Field(description="A few words on what the query answers, for the audit log")


def _probe(backend: TurnBackend) -> BaseTool:
    return StructuredTool.from_function(
        func=lambda: json.dumps(backend.probe()),
        name="probe",
        description=(
            "What the analyzer can see on this database: version, primary or replica, "
            "extensions, privileges and statistics freshness."
        ),
        args_schema=NoArgs,
    )


def _storage(backend: TurnBackend) -> BaseTool:
    return StructuredTool.from_function(
        func=lambda top_n: json.dumps(backend.storage(top_n)),
        name="get_storage_stats",
        description=(
            "Analyse storage: measure every table's size (heap, index, TOAST, total), estimated "
            "rows, vacuum/analyze recency and dead tuples, recorded as an inventory Run. Returns "
            "ranked findings (bloat, stale statistics, index-heavy tables, oversized TOAST), "
            "per-schema totals and the largest top_n tables, largest first."
        ),
        args_schema=StorageArgs,
    )


def _count(backend: TurnBackend) -> BaseTool:
    return StructuredTool.from_function(
        func=lambda tables, all_tables: json.dumps(backend.count_exact(tables, all_tables)),
        name="count_rows_exact",
        description=(
            "Count rows exactly with count(*), only when the user wants exact counts. Each count "
            "runs only if the EXPLAIN gate allows it; tables it refuses are listed under "
            "`skipped` with the reason and keep their estimate. Recorded as an inventory Run "
            "over those tables."
        ),
        args_schema=CountArgs,
    )


def _sql(backend: TurnBackend) -> BaseTool:
    return StructuredTool.from_function(
        func=lambda sql, purpose: json.dumps(backend.sql(sql, purpose)),
        name="run_readonly_sql",
        description=(
            "Run one read-only SELECT for a question no other tool answers. Every output column "
            "must be a count, another aggregate (sum, avg, min, max, ...) of lengths or sizes "
            "only, catalog metadata, or a confirmed entity key: SQL that could return row "
            "values, such as SELECT email or avg(amount), is rejected before it runs. Expensive "
            "plans are refused too."
        ),
        args_schema=SqlArgs,
    )


def _top_queries(backend: TurnBackend) -> BaseTool:
    return StructuredTool.from_function(
        func=lambda top_n: json.dumps(backend.top_queries(top_n)),
        name="get_top_queries",
        description=(
            "Find the slow statements: rank the workload source's statements by total time, "
            "mean time, blocks read and temp blocks written, and explain each from its generic "
            "plan, recorded as a workload Run. Returns the top_n, numbered by rank, each with "
            "why it is slow. Without a workload source, returns a schema review and how to "
            "enable one instead."
        ),
        args_schema=TopQueriesArgs,
    )


def _query_details(backend: TurnBackend) -> BaseTool:
    return StructuredTool.from_function(
        func=lambda rank: json.dumps(backend.query_details(rank)),
        name="get_query_details",
        description=(
            "Everything the latest workload Run recorded about the statement at `rank`: its "
            "full text, statistics, generic plan and the plan rules that fired. Reads data "
            "already collected; sends nothing to the database and makes no Run."
        ),
        args_schema=QueryArgs,
    )


_FACTORIES: list[tuple[Capability, Callable[[TurnBackend], BaseTool]]] = [
    (Capability.PROBE, _probe),
    (Capability.STORAGE_STATS, _storage),
    (Capability.EXACT_COUNTS, _count),
    (Capability.READONLY_SQL, _sql),
    (Capability.WORKLOAD, _top_queries),
    (Capability.WORKLOAD, _query_details),
]


def build_tools(backend: TurnBackend, capabilities: frozenset[Capability]) -> list[BaseTool]:
    return [factory(backend) for cap, factory in _FACTORIES if cap in capabilities]
