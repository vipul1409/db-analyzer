# AnalyzerService and AgentEvent are frozen at v1

**Status:** accepted (#28, M8-01). Closes the freeze half of #26.

**The HTTP API and the web UI are built on `AnalyzerService` and the `AgentEvent` union, so from now on a change to either needs an ADR.** Additions count as changes: a new method or event type is a new obligation for every front end. Fixing a bug without changing a signature or an event's shape does not need one.

**v1 facade.** Connections:
- `add_connection`;
- `connection` (by name);
- `connection_info` and `connections`, each saying whether its DSN variable is set in this process;
- `capabilities`;
- `probe`;
- `latest_probe`, which reads the store and never the database.

Runs:
- `run` (blocking and deterministic);
- `runs`, `run_view`, `run_observations`, `storage`, `workload`;
- `compare_runs`;
- `export`.

Findings: `findings`, `finding_views` (each Finding with its latest Observation), `observations`, `set_finding_status`.

Threads and Turns:
- `start_thread`, `thread`, `threads` (latest activity first, with a preview of the first message);
- `history`, which reads the checkpoint and needs no model key;
- `send`, `cancel`;
- `chat_available`;
- `usage` per Thread, and `usage_in_month` across Threads.

Audit: `audit`.

What the agent's tools and LangGraph Studio use: `run_sql`, `llm_requests`, `record_llm_request`, `studio_graph`.

**v1 events:** `token`, `tool_started`, `tool_finished`, `subagent_started`, `subagent_finished`, `sql_executed`, `sql_rejected`, `run_finished`, `limit_reached`, `usage`, `error`, `done`. They form a union discriminated by `type`. Every field is always sent, and the OpenAPI spec marks each one required.

**What M8-01 changed to get there:**
- **One Turn per Thread, enforced in the facade.** `send` claims the Thread before it returns. A second `send` raises `TurnActive`, which the API maps to 409. The claim is held in process, which is enough because the API is a single process. Dropping an unread stream releases the claim.
- **Cancelling is cooperative.** `cancel` sets an event on the Turn's loop, from any thread. The agent's stream runs in a task of its own, and that task is cancelled at its next await point. The Turn still ends with `usage` and then `done`. `done` gains `cancelled`; `ok` is false for both cancelled and failed Turns. A statement already running in the database finishes within its statement timeout and is audited. The checkpoint stays valid: deepagents patches dangling tool calls on the next Turn. Closing the stream cancels the Turn the same way: the API watches for the client's disconnect and calls `cancel`. Starlette's own disconnect handling is switched off for this stream. uvicorn speaks ASGI 2.3, and there Starlette would tear the stream down first, racing the facade.
- **Subagent events come from the delegating `task` call.** The Translator already sees the orchestrator's `task` tool call, with its `subagent_type`, and its result. `subagent_started` follows that call's `tool_started`. `subagent_finished` (with `ok`) comes before its `tool_finished`. Everything the subagent did streams between the two.
- **A missing DSN variable is its own error** (`DsnEnvMissing`, a `ConnectionRefused`), so the UI can say which variable to set.

**Left out:** `resume` (the entity-map interrupt) returns with the entity map (ADR 0015). `run` stays blocking: a streamed Run with a `run_started` event would need an ADR.

## Considered Options

- **Claim the Turn on the stream's first step:** rejected. The API could not answer 409 before it starts streaming, and two requests could both pass the check.
- **Cancel by throwing into the agent's stream from the consumer:** rejected. LangGraph's stream sets context variables and opens cancel scopes that must stay in one task. A producer task that is cancelled as a whole keeps them there.
- **Derive subagent events from the subgraph namespace:** rejected. The namespace says something runs in a subgraph, but not which subagent it is or when it ended. The `task` call says both.
