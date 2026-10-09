# Agent runtime: the LLM gateway is middleware, and cassettes replay at the model call

**Status:** accepted (#9)

The first agent path (`dbx chat`) settles how the agent reaches the model and how its tests stay offline.

**Gateway as middleware.** The `LLMGateway` (proposal §6.3) is a LangChain agent middleware wrapping every model call, in `agent/llm.py` together with the model factory, `StrictTools` and `TurnLimits`. ADR 0003 and the proposal named that module `agent/llm_factory.py`. This one module is still the single path to OpenAI:
- it caps concurrent calls;
- it writes the redacted request log (`llm_requests`, with tokens and estimated cost per request);
- it totals the turn's usage;
- it records or replays cassettes.

Retries with backoff and timeouts come from the OpenAI SDK, configured by the factory.

**Usage from the gateway, not a callback.** ADR 0003 consequence 6 suggested `UsageMetadataCallbackHandler`. A replayed response never reaches the model, so a callback would see no usage in tests. The gateway reads each response's `usage_metadata` instead. Subagents will share the turn's gateway instance, so their calls are counted too. `AnalyzerService.usage(thread_id)` sums the request log.

**Cassettes at the model call, not HTTP.** Each cassette records the model's responses in call order, and each response carries the shape of its request: the tools offered, the message count, and the last message type. Replay checks that shape and returns the response without calling the model, while tools and SQL run against the fixture for real. A change to the prompt, the tools or the conversation flow fails with `CassetteMismatch` instead of passing silently. Each turn has its own file, `turn-N.json`, where N comes from the Thread's state, so a Thread resumed after a restart replays the right turn.

**One HTTP client per turn.** langchain-openai otherwise shares one async HTTP client across event loops, and each `dbx chat` turn runs in its own loop. Every turn after the first then failed with "Event loop is closed".

**Turn limits from the messages.** `TurnLimits` counts the turn's tokens and tool calls from the messages after the user's last message, so the counts survive checkpoints. A reply that crosses the token limit ends the turn before its tool calls run. A tool call beyond the call limit is refused, not run.

## Considered Options

- **HTTP-level VCR (vcrpy) for the OpenAI client:** rejected. Streaming Responses-API traffic records poorly, and request bodies contain run ids and sizes that differ on every run, so matching would need custom scrubbing. Replaying at the model call avoids both.
- **One cassette file per test:** rejected. A restart test runs a turn in a second service instance, which would replay from the start of the file.

## Consequences

- Re-record cassettes after changing a prompt, a tool or the model (README, Development).
- The deepagents summarization middleware calls the model outside the gateway when a conversation grows long. That path is neither logged nor replayed yet.
- LangGraph Studio reuses one graph per server session: one Thread records its SQL and requests, and runs that overlap in time share a query cap.
