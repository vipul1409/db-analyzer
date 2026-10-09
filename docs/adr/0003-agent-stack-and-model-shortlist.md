# Agent stack: go with deepagents + ChatOpenAI; shortlist gpt-6.1-sol, gpt-5.4-mini, gpt-6-luna

**Status:** accepted (spike #5)

`deepagents` 0.7.23 with `langchain-openai` 1.7 (Responses API) and `langgraph-checkpoint-sqlite` 3.1 does what the proposal assumes. The spike (`tests/spikes/test_agent_stack.py`) passed on all three shortlisted models, though gpt-6.1-sol only intermittently (see consequence 8). In each passing run the orchestrator called a strict-schema tool, delegated to a subagent that returned a Pydantic `IndexReport` via structured output, and then answered a follow-up in a **new process** from the SQLite checkpoint without calling any tools.

## Measured tokens and cost per turn (9 Oct 2026 prices)

Turn 0 is a tool-using turn: orchestrator → `table_sizes` → `task` → subagent → `index_count` → structured output → answer. Turn 1 is a follow-up answered from memory.

| Model | $/1M in · cached · out | Turn 0 tokens in (cached) / out | Turn 0 cost (measured · cold) | Turn 1 tokens in (cached) / out | Turn 1 cost (measured · cold) |
|---|---|---|---|---|---|
| gpt-6.1-sol | 2.00 · 0.10 · 10.00 | 10,097 (9,835) / 309 | $0.0046 · $0.023 | 2,374 (2,234) / 26 | $0.0008 · $0.0050 |
| gpt-5.4-mini | 0.75 · 0.075 · 4.50 | 10,048 (8,704) / 213 | $0.0026 · $0.0085 | 2,325 (2,048) / 10 | $0.0004 · $0.0018 |
| gpt-6-luna | 0.10 · 0.01 · 0.50 | 11,943 (11,660) / 286 | $0.0003 · $0.0013 | 2,314 (2,199) / 10 | <$0.0001 · $0.0002 |

"Measured" includes OpenAI's automatic prompt cache, which served 87–98% of input tokens, because the deepagents prompt and tool schemas form a stable prefix. "Cold" prices every input token at the full rate (worst case).

**Shortlist** (choice made by the M7 eval suite, not here):
- **gpt-6.1-sol:** orchestrator candidate.
- **gpt-5.4-mini:** middle option, for the orchestrator or subagents.
- **gpt-6-luna:** subagent candidate. It cost about 15× less per turn than gpt-6.1-sol (20× on list price) and got this task right.

`gpt-6-astra` ($10/$50 per 1M in/out) and `gpt-5.5` ($5/$30), from the same pricing page, were left out on price. If the eval suite shows gpt-6.1-sol falls short, they are the next candidates.

## Consequences

Lessons from the spike that shape M2:

1. **Strict schemas aren't on by default.** Through the Responses API, langchain sets `strict` only when asked, and `create_deep_agent` has no parameter for it. A middleware (`wrap_model_call` → `request.override(model_settings={"strict": True})`) turns it on for every tool, including the built-in filesystem tools, whose optional arguments become required-but-nullable. `strict: true` reaching the request payload (for `table_sizes` and `read_file`) was checked by hand with `ChatOpenAI._get_request_payload`; the spike test doesn't assert it. The tool factory (M2-03) must install this middleware on the orchestrator and on every subagent.
2. **Null tool results become invented numbers.** When the fake `index_count` got `shop.audit_log` and returned `{"index_count": null}`, both gpt-6-luna and gpt-5.4-mini reported **0 indexes** and called the table under-indexed: the strict `int` field left no way to say "unknown". Tools must return an explicit error that names the valid input, never a silent null. Structured-output schemas need an explicit unknown/`None` where a value may be missing. Models also schema-qualify table names unprompted, so tools should accept `schema.table`. Add this case to the M7 evals.
3. **Built-ins have to be trimmed through a global profile.** `execute` (shell) and the auto-added `general-purpose` subagent, which would inherit every tool, are removed with `register_harness_profile("openai", HarnessProfile(excluded_tools={"execute"}, general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))`. That is process-global, beta API. Keep it in one place (`agent/llm_factory.py`) and pin `deepagents`. The filesystem tools (`ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, `grep`) stay; they work on the in-state virtual filesystem the proposal wants for large results.
4. **No todo planner by default.** deepagents 0.7 no longer adds `write_todos`. Add langchain's `TodoListMiddleware` to the orchestrator for the "plan todos for a full analysis" behaviour (§6.2).
5. **Fixed overhead is roughly 2–2.5k input tokens per model call.** That is the deepagents prompt and the tool schemas; turn 1 (one call, short history) was ~2.3k, and a tool-using turn makes 4–5 calls (≈10k tokens). Caching absorbs most of it while the prefix stays stable, so tool schemas and system prompts must not vary per turn (§6.3).
6. **Usage tracking.** `UsageMetadataCallbackHandler` passed in `config["callbacks"]` captures the subagent's model calls as well as the orchestrator's, keyed by the dated model name (for example `gpt-5.4-mini-2026-03-17`). This is enough for M2-02. Cost lookup must match on the model-name prefix.
7. **Data retention.** `init_chat_model(..., use_responses_api=True, store=False)` works with tools, subagents and structured output.
8. **Native structured output on gpt-6.1-sol sometimes fails to parse.** On later reruns the subagent's `IndexReport` raised `StructuredOutputValidationError` ("expected valid JSON … Expecting value: line 1 column 1"), which crashed the whole turn. That happened in 3 of 6 runs, then 0 of 6 in the next batch. Successful runs return a reasoning block followed by a JSON text block. The cause wasn't diagnosed (an empty or missing text block is likely). It wasn't seen on gpt-5.4-mini or gpt-6-luna. M2 must not let one bad structured response kill a turn: retry it, or fall back to `ToolStrategy`. Add a repeated-run check to the M7 evals.

## Not covered by this spike

- **Mixed models.** The subagent always ran on the orchestrator's model. A cheap subagent model under a stronger orchestrator (`subagent_overrides`, proposal §6.3) is untested; the `SubAgent.model` field accepts it, but verify it in M2.
- **Orchestrator-level structured output.** Only the subagent's `response_format` was exercised. The entity-map proposal and findings summary (§6.3) use structured output on the orchestrator; check that in M6.

## Considered Options

- **Plain LangGraph without deepagents:** rejected for now. deepagents supplies the subagent `task` tool, the virtual filesystem and the summarization the proposal relies on, and its rough edges (consequences 1, 3, 4 and 8) have small workarounds.
- **Chat Completions instead of the Responses API:** not needed. langchain-openai sets `strict` automatically there only when a `response_format` is present, so it would need the same middleware anyway.
