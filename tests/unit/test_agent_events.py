from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from db_analyzer.agent.events import (
    TURN_LIMIT,
    LimitReached,
    SqlExecuted,
    SqlRejected,
    Token,
    ToolFinished,
    ToolStarted,
    Translator,
)


def messages(msg: Any, node: str = "model") -> tuple[str, Any]:
    return ("messages", (msg, {"langgraph_node": node}))


def updates(*msgs: Any, node: str = "model") -> tuple[str, Any]:
    return ("updates", {node: {"messages": list(msgs)}})


def test_streamed_chunks_become_tokens() -> None:
    t = Translator()

    events = [
        *t.translate(*messages(AIMessageChunk(content="The ", id="m1"))),
        *t.translate(*messages(AIMessageChunk(content="biggest", id="m1"))),
    ]

    assert events == [Token(text="The "), Token(text="biggest")]
    assert t.answer == "The biggest"


def test_tool_messages_and_human_input_are_not_tokens() -> None:
    t = Translator()

    assert t.translate(*messages(ToolMessage(content="{}", tool_call_id="c1"), node="tools")) == []
    assert t.translate(*messages(HumanMessage(content="hi"), node="model")) == []


def test_tool_calls_start_and_finish() -> None:
    t = Translator()
    call = {"name": "get_storage_stats", "args": {"top_n": 5}, "id": "c1"}

    started = t.translate(*updates(AIMessage(content="", tool_calls=[call])))
    finished = t.translate(
        *updates(
            ToolMessage(content='{"tables": []}', tool_call_id="c1", name="get_storage_stats"),
            node="tools",
        )
    )

    assert started == [ToolStarted(call_id="c1", name="get_storage_stats", args={"top_n": 5})]
    assert finished == [
        ToolFinished(call_id="c1", name="get_storage_stats", ok=True, summary='{"tables": []}')
    ]


def test_failed_tool_is_reported_as_not_ok() -> None:
    msg = ToolMessage(content="boom", tool_call_id="c1", name="probe", status="error")

    [event] = Translator().translate(*updates(msg, node="tools"))

    assert isinstance(event, ToolFinished) and event.ok is False


def test_sql_events_pass_through_from_tools() -> None:
    t = Translator()
    executed = SqlExecuted(
        sql="SELECT 1", purpose="inventory", duration_ms=1.0, row_count=1, plan_cost=None
    )
    rejected = SqlRejected(sql="SELECT pg_sleep(1)", purpose="agent", reason="no")

    assert t.translate("custom", executed.model_dump()) == [executed]
    assert t.translate("custom", rejected.model_dump()) == [rejected]


def test_turn_limit_message_becomes_a_limit_event() -> None:
    msg = AIMessage(
        content="Stopped: token limit",
        name=TURN_LIMIT,
        response_metadata={"limit": "tokens", "used": 250, "max": 200},
    )

    events = Translator().translate(*updates(msg, node="TurnLimits.before_model"))

    assert events == [LimitReached(limit="tokens", used=250, max=200)]


def test_custom_payloads_that_are_not_agent_events_are_ignored() -> None:
    assert Translator().translate("custom", {"rubric": "score"}) == []
    assert Translator().translate("custom", "progress") == []


def test_answer_is_the_text_after_the_last_tool_call() -> None:
    t = Translator()
    call = {"name": "probe", "args": {}, "id": "c1"}
    t.translate(*messages(AIMessageChunk(content="Let me check. ", id="m1")))
    t.translate(*updates(AIMessage(content="Let me check. ", tool_calls=[call], id="m1")))
    t.translate(*messages(AIMessageChunk(content="public.events", id="m2")))

    assert t.answer == "public.events"
