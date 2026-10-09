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


ALL = frozenset(Capability)


def test_each_capability_gives_its_tool() -> None:
    names = [t.name for t in build_tools(FakeBackend(), ALL)]

    assert names == ["probe", "get_storage_stats"]


def test_connection_without_a_capability_has_no_tool_for_it() -> None:
    names = [t.name for t in build_tools(FakeBackend(), frozenset({Capability.PROBE}))]

    assert names == ["probe"]


def test_tools_call_the_backend_and_return_json() -> None:
    backend = FakeBackend()
    probe, storage = build_tools(backend, ALL)

    assert json.loads(probe.invoke({})) == {"server": "PostgreSQL 17"}
    assert json.loads(storage.invoke({"top_n": 1})) == {"tables": [{"table": "public.events"}]}
    assert backend.calls == [("probe", None), ("storage", 1)]


def test_tool_arguments_have_a_strict_json_schema() -> None:
    from langchain_core.utils.function_calling import convert_to_openai_tool

    _, storage = build_tools(FakeBackend(), ALL)

    schema = convert_to_openai_tool(storage, strict=True)["function"]["parameters"]
    assert schema["required"] == ["top_n"]
    assert schema["additionalProperties"] is False
