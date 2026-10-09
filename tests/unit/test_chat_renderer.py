import io

from rich.console import Console

from db_analyzer.agent.events import (
    Done,
    Error,
    LimitReached,
    SqlExecuted,
    SqlRejected,
    Token,
    ToolFinished,
    ToolStarted,
    Usage,
)
from db_analyzer.cli import TurnRenderer


def render(*events: object) -> str:
    out = io.StringIO()
    renderer = TurnRenderer(Console(file=out, width=120, color_system=None))
    for e in events:
        renderer.render(e)  # type: ignore[arg-type]
    return out.getvalue()


def test_tokens_stream_on_one_line() -> None:
    assert render(Token(text="The "), Token(text="biggest"), Done(thread_id="t", answer="")) == (
        "The biggest\n"
    )


def test_tool_and_sql_events_are_shown() -> None:
    text = render(
        ToolStarted(call_id="c1", name="get_storage_stats", args={"top_n": 5}),
        SqlExecuted(
            sql="SELECT n.nspname\nFROM pg_class",
            purpose="inventory",
            duration_ms=3.2,
            row_count=8,
            plan_cost=None,
        ),
        SqlRejected(
            sql="SELECT pg_sleep(1)", purpose="agent", reason="function pg_sleep is not allowed"
        ),
        ToolFinished(call_id="c1", name="get_storage_stats", ok=True, summary="{}"),
    )

    assert "get_storage_stats(top_n=5)" in text
    assert "inventory" in text and "8 rows" in text and "SELECT n.nspname" in text
    assert "rejected" in text and "function pg_sleep is not allowed" in text


def test_failures_limits_and_usage_are_shown() -> None:
    text = render(
        ToolFinished(call_id="c1", name="probe", ok=False, summary="database error"),
        LimitReached(limit="tool_calls", used=61, max=60),
        Error(message="RuntimeError: boom"),
        Usage(
            model="gpt-5.4-mini",
            input_tokens=4611,
            cached_tokens=3072,
            output_tokens=312,
            cost_usd=0.0028,
        ),
    )

    assert "probe failed: database error" in text
    assert "tool_calls limit" in text and "61 of 60" in text
    assert "RuntimeError: boom" in text
    assert "4,611 in" in text and "$0.0028" in text
