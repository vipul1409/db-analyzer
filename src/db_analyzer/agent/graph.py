"""The deepagents orchestrator graph."""

from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

SYSTEM_PROMPT = """\
You analyse one PostgreSQL database for an engineer, through read-only tools.

- Every number in your answer comes from a tool result in this conversation. Never estimate.
- Quote sizes exactly as the tool's *_pretty fields give them.
- Row counts from get_storage_stats are planner estimates: say so.
- If a tool returns an error, say what failed; do not fill the gap yourself.
- Answer briefly: a ranked list or a small table, then one line of interpretation.
"""

# No shell, and no catch-all subagent that would inherit every tool (ADR 0003). Process-wide.
register_harness_profile(
    "openai",
    HarnessProfile(
        excluded_tools=frozenset({"execute"}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ),
)


def build_agent(
    model: BaseChatModel,
    tools: list[BaseTool],
    middleware: list[Any],
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> Any:
    """`middleware` wraps model calls outermost first: turn limits, strict schemas, gateway."""
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        checkpointer=checkpointer,
        name="db_analyzer",
    )
