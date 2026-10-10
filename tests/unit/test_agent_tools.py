import json
from typing import Any

from db_analyzer.agent.tools import build_tools
from db_analyzer.core.model import Capability


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def probe(self) -> dict[str, Any]:
        self.calls.append(("probe", None))
        return {"server": "PostgreSQL 17"}

    def storage(self, top_n: int) -> dict[str, Any]:
        self.calls.append(("storage", top_n))
        return {"tables": [{"table": "public.events"}][:top_n]}

    def count_exact(self, tables: list[str], all_tables: bool) -> dict[str, Any]:
        self.calls.append(("count", (tables, all_tables)))
        return {"counted": [{"table": "public.tenants", "rows": 20}]}

    def sql(self, sql: str, purpose: str) -> dict[str, Any]:
        self.calls.append(("sql", (sql, purpose)))
        return {"columns": ["n"], "rows": [[3]]}

    def top_queries(self, top_n: int) -> dict[str, Any]:
        self.calls.append(("top_queries", top_n))
        return {"queries": [{"rank": 1}][:top_n]}

    def query_details(self, rank: int) -> dict[str, Any]:
        self.calls.append(("query_details", rank))
        return {"rank": rank}


ALL = frozenset(Capability)


def test_each_capability_gives_its_tool() -> None:
    names = [t.name for t in build_tools(FakeBackend(), ALL)]

    assert names == [
        "probe",
        "get_storage_stats",
        "count_rows_exact",
        "run_readonly_sql",
        "get_top_queries",
        "get_query_details",
    ]


def test_connection_without_a_capability_has_no_tool_for_it() -> None:
    names = [t.name for t in build_tools(FakeBackend(), frozenset({Capability.PROBE}))]

    assert names == ["probe"]


def test_tools_call_the_backend_and_return_json() -> None:
    backend = FakeBackend()
    probe, storage, count, sql, top, details = build_tools(backend, ALL)

    assert json.loads(probe.invoke({})) == {"server": "PostgreSQL 17"}
    assert json.loads(storage.invoke({"top_n": 1})) == {"tables": [{"table": "public.events"}]}
    exact = {"tables": ["public.tenants"], "all_tables": False}
    assert json.loads(count.invoke(exact))["counted"][0]["rows"] == 20
    query = {"sql": "SELECT count(*) AS n FROM t", "purpose": "count t"}
    assert json.loads(sql.invoke(query)) == {"columns": ["n"], "rows": [[3]]}
    assert json.loads(top.invoke({"top_n": 1})) == {"queries": [{"rank": 1}]}
    assert json.loads(details.invoke({"rank": 3})) == {"rank": 3}
    assert backend.calls == [
        ("probe", None),
        ("storage", 1),
        ("count", (["public.tenants"], False)),
        ("sql", ("SELECT count(*) AS n FROM t", "count t")),
        ("top_queries", 1),
        ("query_details", 3),
    ]


def test_tool_arguments_have_a_strict_json_schema() -> None:
    from langchain_core.utils.function_calling import convert_to_openai_tool

    _, storage, count, sql, top, details = build_tools(FakeBackend(), ALL)

    schema = convert_to_openai_tool(storage, strict=True)["function"]["parameters"]
    assert schema["required"] == ["top_n"]
    assert schema["additionalProperties"] is False
    schema = convert_to_openai_tool(sql, strict=True)["function"]["parameters"]
    assert schema["required"] == ["sql", "purpose"]
    schema = convert_to_openai_tool(count, strict=True)["function"]["parameters"]
    assert schema["required"] == ["tables", "all_tables"]
    schema = convert_to_openai_tool(top, strict=True)["function"]["parameters"]
    assert schema["required"] == ["top_n"]
    schema = convert_to_openai_tool(details, strict=True)["function"]["parameters"]
    assert schema["required"] == ["rank"]


def test_the_workload_capability_gives_both_workload_tools() -> None:
    names = [t.name for t in build_tools(FakeBackend(), frozenset({Capability.WORKLOAD}))]

    assert names == ["get_top_queries", "get_query_details"]
