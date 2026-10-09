"""The deepagents orchestrator graph and its subagents."""

from collections.abc import Sequence
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    SubAgent,
    create_deep_agent,
    register_harness_profile,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

SYSTEM_PROMPT = """\
You analyse one PostgreSQL database for an engineer, through read-only tools and subagents.

- Storage questions (table sizes, biggest tables, bloat, dead tuples, statistics freshness,
  vacuum/analyze, TOAST, index size, row counts, "analyse storage") go to the
  inventory-analyst subagent through the task tool. Pass on the user's question and any tables
  they named; ask for exact row counts only when the user wants them.
- Every number in your answer comes from a tool or subagent result in this conversation. Never
  estimate. Quote sizes exactly as given.
- Prefer the dedicated tools and subagents. Use run_readonly_sql only for what they do not
  answer; it returns aggregates, catalog metadata and entity keys, never row values.
- If a tool or subagent reports an error or a skipped measurement, say what and why; do not
  fill the gap yourself. A rejected query may be retried once in a form the error allows.
- Answer briefly: ranked findings or a small table, then one line of interpretation.
"""

INVENTORY_ANALYST = "inventory-analyst"
INVENTORY_PROMPT = """\
You are the inventory analyst for one PostgreSQL database. You report to an orchestrator, who
answers the user from your report.

- Call get_storage_stats once for the question you are given (top_n 10 unless asked otherwise).
  Its findings are already ranked, most severe first; keep that order.
- Call count_rows_exact only when exact row counts were asked for, with the tables named (or
  all_tables when all were asked for). Counts the gate refused stay estimates: report the reason.
- Report: the ranked findings (severity, finding, title, recommendation), then the largest
  tables (total, heap, indexes, TOAST, rows with their method: estimate or exact), then the
  schema totals when there is more than one schema, then anything skipped and why.
- Use only numbers from tool results; quote sizes exactly as the *_pretty fields give them.
  If a tool returns an error, report it; do not fill the gap.
"""
INVENTORY_TOOLS = ("probe", "get_storage_stats", "count_rows_exact")
ORCHESTRATOR_TOOLS = ("probe", "run_readonly_sql")

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
    orchestrator_only: Sequence[Any] = (),
) -> Any:
    """`tools` are those the Connection's capabilities allow; each goes to the orchestrator or
    the subagent that owns it. `middleware` wraps every agent's model calls, outermost first
    (turn limits, strict schemas, gateway); `orchestrator_only` goes before it on the
    orchestrator alone (e.g. per-turn resets)."""
    by_name = {t.name: t for t in tools}
    subagents: list[SubAgent] = []
    if "get_storage_stats" in by_name:
        subagents.append(
            {
                "name": INVENTORY_ANALYST,
                "description": (
                    "Analyses storage: table and schema sizes, partitions, row counts (exact "
                    "on request), vacuum/analyze recency, bloat, stale statistics, index-heavy "
                    "tables and oversized TOAST. Returns ranked findings with the numbers."
                ),
                "system_prompt": INVENTORY_PROMPT,
                "tools": [by_name[n] for n in INVENTORY_TOOLS if n in by_name],
                "model": model,
                "middleware": list(middleware),
            }
        )
    return create_deep_agent(
        model=model,
        tools=[by_name[n] for n in ORCHESTRATOR_TOOLS if n in by_name],
        system_prompt=SYSTEM_PROMPT,
        middleware=[*orchestrator_only, *middleware],
        subagents=subagents,
        checkpointer=checkpointer,
        name="db_analyzer",
    )
