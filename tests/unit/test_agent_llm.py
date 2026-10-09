from pathlib import Path
from typing import Any, cast

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from db_analyzer.agent.events import TURN_LIMIT
from db_analyzer.agent.llm import (
    Cassette,
    CassetteExhausted,
    CassetteMismatch,
    LLMGateway,
    LLMSettings,
    PrivacyViolation,
    TurnLimits,
    redact,
)
from db_analyzer.core.model import LLMRequestLog
from db_analyzer.safety.aliases import Aliases


class Fake(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> "Fake":
        return self


class Exploding(Fake):
    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the network must not be used in replay")


@tool
def sizes(top_n: int) -> str:
    """Table sizes."""
    return '{"tables": ["public.events"]}'


def call(n: int = 1) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "sizes", "args": {"top_n": 3}, "id": f"c{i}"} for i in range(n)],
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    )


ANSWER = AIMessage(
    content="public.events is the biggest",
    usage_metadata={"input_tokens": 120, "output_tokens": 8, "total_tokens": 128},
    response_metadata={"model_name": "gpt-5.4-mini-2026-03-17"},
)


def run(model: Any, *middleware: Any) -> list[Any]:
    agent = create_agent(model, tools=[sizes], middleware=list(middleware))
    state: Any = {"messages": [("user", "biggest tables?")]}
    return list(agent.invoke(state)["messages"])


def test_redact_removes_credentials() -> None:
    text = "postgresql://db_analyzer:s3cret@host/shop and key sk-proj-abcdefghijklmnop1234"

    assert redact(text) == "postgresql://db_analyzer:***@host/shop and key sk-***"


def test_recorded_cassette_replays_without_the_network(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    recorder = LLMGateway(LLMSettings(), cassette=Cassette(path, record=True))
    recorded = run(Fake(messages=iter([call(), ANSWER])), recorder)

    replayer = LLMGateway(LLMSettings(), cassette=Cassette(path, record=False))
    replayed = run(Exploding(messages=iter([])), replayer)

    assert replayed[-1].text == recorded[-1].text == "public.events is the biggest"


def test_replay_fails_when_the_request_no_longer_matches_the_recording(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    run(
        Fake(messages=iter([call(), ANSWER])),
        LLMGateway(LLMSettings(), cassette=Cassette(path, True)),
    )

    agent = create_agent(
        Exploding(messages=iter([])),
        tools=[],
        middleware=[LLMGateway(LLMSettings(), cassette=Cassette(path, record=False))],
    )  # the tools offered changed since recording
    with pytest.raises(CassetteMismatch, match="tools"):
        agent.invoke(cast(Any, {"messages": [("user", "biggest tables?")]}))


def test_replay_past_the_end_of_the_cassette_fails(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    run(Fake(messages=iter([ANSWER])), LLMGateway(LLMSettings(), cassette=Cassette(path, True)))

    gateway = LLMGateway(LLMSettings(), cassette=Cassette(path, record=False))
    run(Exploding(messages=iter([])), gateway)  # uses the one recorded response
    with pytest.raises(CassetteExhausted):
        run(Exploding(messages=iter([])), gateway)


def test_gateway_logs_redacted_requests_and_totals_usage() -> None:
    log: list[LLMRequestLog] = []
    gateway = LLMGateway(LLMSettings(model="gpt-5.4-mini"), log=log.append)

    run(Fake(messages=iter([call(), ANSWER])), gateway)

    assert len(log) == 2
    assert "biggest tables?" in log[0].request
    assert log[1].output_tokens == 8
    usage = gateway.usage()
    assert (usage.input_tokens, usage.output_tokens) == (220, 18)
    assert usage.cost_usd == pytest.approx((220 * 0.75 + 18 * 4.50) / 1e6)


def test_token_limit_ends_the_turn_before_the_next_model_call() -> None:
    messages = run(
        Fake(messages=iter([call(), ANSWER])), TurnLimits(max_tokens=100, max_tool_calls=10)
    )

    last = messages[-1]
    assert last.name == TURN_LIMIT
    assert last.response_metadata == {"limit": "tokens", "used": 110, "max": 100}


def test_tool_calls_over_the_limit_are_not_run_and_end_the_turn() -> None:
    messages = run(Fake(messages=iter([call(3), ANSWER])), TurnLimits(10_000, max_tool_calls=2))

    results = [m for m in messages if isinstance(m, ToolMessage)]
    assert [m.status for m in results] == ["success", "success", "error"]
    assert "limit" in str(results[-1].content)
    assert messages[-1].response_metadata == {"limit": "tool_calls", "used": 3, "max": 2}


def test_turn_within_limits_is_untouched() -> None:
    messages = run(Fake(messages=iter([call(), ANSWER])), TurnLimits(10_000, 10))

    assert messages[-1].text == "public.events is the biggest"


@tool
def leaky(top_n: int) -> str:
    """A tool with a bug: it returns row data."""
    return '{"accounts": [{"email": "user5@example.test"}]}'


def test_gateway_withholds_a_tool_result_that_carries_row_data() -> None:
    log: list[LLMRequestLog] = []
    gateway = LLMGateway(LLMSettings(), log=log.append)
    agent = create_agent(
        Fake(messages=iter([call_tool("leaky"), ANSWER])), tools=[leaky], middleware=[gateway]
    )

    messages = agent.invoke(cast(Any, {"messages": [("user", "biggest tables?")]}))["messages"]

    [result] = [m for m in messages if isinstance(m, ToolMessage)]
    assert result.status == "error" and "privacy" in str(result.content)
    assert not any("user5@example.test" in str(m.content) for m in messages)
    assert not any("user5@example.test" in entry.request for entry in log)


def test_gateway_refuses_to_send_row_data_already_in_the_conversation() -> None:
    gateway = LLMGateway(LLMSettings())
    agent = create_agent(Exploding(messages=iter([])), tools=[sizes], middleware=[gateway])
    history = [
        HumanMessage("which account is busiest?"),
        call_tool("sizes"),
        ToolMessage('{"email": "user5@example.test"}', tool_call_id="c0", name="sizes"),
        HumanMessage("and the next one?"),
    ]

    with pytest.raises(PrivacyViolation, match="email address"):
        agent.invoke(cast(Any, {"messages": history}))


def call_tool(name: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": {"top_n": 1}, "id": "c0"}])


def test_gateway_shows_the_model_aliases_and_gives_tools_real_names() -> None:
    seen: list[str] = []

    @tool
    def count_rows(table: str) -> str:
        """Rows in one table."""
        seen.append(table)
        return '{"table": "public.events", "rows": 60000}'

    log: list[LLMRequestLog] = []
    gateway = LLMGateway(
        LLMSettings(),
        log=log.append,
        aliases=Aliases({"public": "schema_1", "events": "table_1"}),
    )
    hidden_call = AIMessage(
        content="",
        tool_calls=[{"name": "count_rows", "args": {"table": "schema_1.table_1"}, "id": "c0"}],
    )
    agent = create_agent(
        Fake(messages=iter([hidden_call, ANSWER])), tools=[count_rows], middleware=[gateway]
    )

    agent.invoke(cast(Any, {"messages": [("user", "how many rows in events?")]}))

    assert seen == ["public.events"]
    sent = "\n".join(entry.request for entry in log)
    assert "how many rows in table_1?" in sent
    assert "schema_1.table_1" in sent
    assert "events" not in sent
