"""Spike (#5): does deepagents + ChatOpenAI give us the agent stack the proposal assumes?

Checks, per shortlisted OpenAI model: two tools with strict JSON schemas, one subagent that
returns Pydantic structured output, and a SQLite-checkpointed thread that survives a process
restart (each turn runs in its own Python process). Records tokens and cost per turn.

Throwaway. Needs: OPENAI_API_KEY (see .env.example). Costs a few cents per run.
Run:           uv run --env-file .env pytest -m spike -s tests/spikes/test_agent_stack.py
Results are recorded in docs/adr/0003-agent-stack-and-model-shortlist.md.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest
from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    SubAgent,
    create_deep_agent,
    register_harness_profile,
)
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, Field

pytestmark = pytest.mark.spike

# USD per 1M tokens: (input, cached input, output).
# Source: developers.openai.com/api/docs/pricing, 9 Oct 2026.
PRICES = {
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
}
TURNS = [
    "Which three tables in schema 'shop' are the largest? Then ask the inventory-analyst "
    "how many indexes the largest one has, and tell me both.",
    "Without calling any tools: which table did you say was the largest, and how many "
    "indexes did the analyst report for it? Reply as '<table>: <n> indexes'.",
]

# Fake catalog: the spike tests the agent stack, not the database layer.
SIZES = {"audit_log": 7_900, "orders": 5_200, "order_items": 3_100, "customers": 400}
INDEXES = {"audit_log": 2, "orders": 6, "order_items": 4, "customers": 3}


class TableSizesArgs(BaseModel):
    schema_name: str = Field(description="Schema to inspect")
    top_n: int = Field(description="How many tables to return, largest first")


class IndexCountArgs(BaseModel):
    table: str = Field(description="Unqualified table name")


class IndexReport(BaseModel):
    """Structured output of the inventory-analyst subagent."""

    table: str
    index_count: int
    verdict: Literal["ok", "under_indexed", "over_indexed"]


@tool(args_schema=TableSizesArgs)
def table_sizes(schema_name: str, top_n: int) -> str:
    """Return the largest tables in a schema with their total size in MB."""
    top = sorted(SIZES.items(), key=lambda kv: -kv[1])[:top_n]
    return json.dumps([{"table": t, "total_mb": mb} for t, mb in top])


@tool(args_schema=IndexCountArgs)
def index_count(table: str) -> str:
    """Return the number of indexes on a table."""
    if table not in INDEXES:
        # Returning {"index_count": null} here made gpt-6-luna and gpt-5.4-mini report 0:
        # the strict int field left them no way to say "unknown". Tools must fail loudly.
        return json.dumps({"error": f"unknown table {table!r}; pass one of {sorted(INDEXES)}"})
    return json.dumps({"table": table, "index_count": INDEXES[table]})


class StrictTools(AgentMiddleware[Any, Any, Any]):
    """Force strict JSON schemas on every tool bind and record the tools the model sees.

    With the Responses API (deepagents' OpenAI default) langchain only sets `strict`
    when asked, and create_deep_agent has no parameter for it.
    """

    seen: ClassVar[set[str]] = set()

    def wrap_model_call(self, request: ModelRequest[Any], handler: Any) -> ModelResponse[Any]:
        StrictTools.seen |= {t["name"] if isinstance(t, dict) else t.name for t in request.tools}
        return handler(request.override(model_settings={**request.model_settings, "strict": True}))  # type: ignore[no-any-return]


def build_agent(model_name: str, checkpointer: SqliteSaver) -> Any:
    # No shell, and no catch-all subagent that would inherit every tool (proposal §6.2).
    register_harness_profile(
        "openai",
        HarnessProfile(
            excluded_tools=frozenset({"execute"}),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    model = init_chat_model(f"openai:{model_name}", use_responses_api=True, store=False)
    analyst: SubAgent = {
        "name": "inventory-analyst",
        "description": "Counts indexes on one table and judges whether that is reasonable.",
        "system_prompt": "Call index_count for the table you are given, then judge it.",
        "tools": [index_count],
        "model": model,
        "middleware": [StrictTools()],
        "response_format": IndexReport,
    }
    return create_deep_agent(
        model=model,
        tools=[table_sizes],
        system_prompt="You analyse a PostgreSQL database. Every number must come from a tool.",
        subagents=[analyst],
        middleware=[StrictTools()],
        checkpointer=checkpointer,
    )


def run_turn(model_name: str, db: str, thread: str, turn: int) -> dict[str, Any]:
    """One user turn in a fresh process: rebuild the agent, resume the thread from SQLite."""
    usage = UsageMetadataCallbackHandler()
    with SqliteSaver.from_conn_string(db) as checkpointer:
        agent = build_agent(model_name, checkpointer)
        config: Any = {"configurable": {"thread_id": thread}, "callbacks": [usage]}
        before = agent.get_state(config).values.get("messages", [])
        state = agent.invoke({"messages": [("user", TURNS[turn])]}, config)
    new = state["messages"][len(before) :]
    tool_calls = [c["name"] for m in new if isinstance(m, AIMessage) for c in m.tool_calls]
    reports = [
        IndexReport.model_validate_json(str(m.content))
        for m in new
        if isinstance(m, ToolMessage) and m.name == "task"
    ]
    return {
        "history": len(before),
        "tool_calls": tool_calls,
        "reports": [r.model_dump() for r in reports],
        "answer": state["messages"][-1].text,
        "tools_seen": sorted(StrictTools.seen),
        "usage": {name: dict(u) for name, u in usage.usage_metadata.items()},
    }


def cost(usage: dict[str, Any]) -> tuple[int, int, int, float]:
    tokens_in = tokens_cached = tokens_out = 0
    usd = 0.0
    for name, u in usage.items():
        price_in, price_cached, price_out = next(p for m, p in PRICES.items() if name.startswith(m))
        cached = u.get("input_token_details", {}).get("cache_read", 0)
        tokens_in += u["input_tokens"]
        tokens_cached += cached
        tokens_out += u["output_tokens"]
        usd += (
            (u["input_tokens"] - cached) * price_in
            + cached * price_cached
            + u["output_tokens"] * price_out
        ) / 1e6
    return tokens_in, tokens_cached, tokens_out, usd


@pytest.mark.parametrize("model_name", PRICES)
def test_two_turns_survive_restart(model_name: str, tmp_path: Path) -> None:
    db = str(tmp_path / "checkpoints.sqlite")
    results = []
    for turn in range(len(TURNS)):
        out = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.spikes.test_agent_stack",
                model_name,
                db,
                "t1",
                str(turn),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert out.returncode == 0, out.stderr[-3000:]
        results.append(json.loads(out.stdout.splitlines()[-1]))

    first, second = results
    print(f"\n== {model_name}")
    for i, r in enumerate(results):
        tin, tcached, tout, usd = cost(r["usage"])
        print(
            f"turn {i}: in={tin} (cached {tcached}) out={tout} ${usd:.4f} tools={r['tool_calls']}"
        )
        print(f"  answer: {r['answer'][:300]!r}")
    print(f"  tools offered: {first['tools_seen']}")

    assert "table_sizes" in first["tool_calls"] and "task" in first["tool_calls"]
    assert first["reports"] and first["reports"][0]["table"].endswith("audit_log")
    assert first["reports"][0]["index_count"] == 2
    assert "execute" not in first["tools_seen"]
    assert second["history"] > 0, "thread was not restored from the checkpointer"
    assert second["tool_calls"] == []
    assert re.search(r"audit_log\D*\b2 index", second["answer"]), second["answer"]


if __name__ == "__main__":
    model_name, db, thread, turn = sys.argv[1:]
    print(json.dumps(run_turn(model_name, db, thread, int(turn)), default=str))
