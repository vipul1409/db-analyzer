# Pause agent work; build the API and UI on the current agent

**Status:** accepted (re-plan, 10 Oct 2026). Changes the execution plan's "agent first" order.

**The agent stops where it is, and the API and UI are built on it now.** The agent has these today:
- storage and index-health Runs;
- workload ranking from `pg_stat_statements` or Azure Query Store, with plan rules;
- the Finding lifecycle, and comparison between Runs;
- the audit log;
- chat with the inventory and workload subagents.

That is enough to be useful through a browser. The rest of Phase A waits:
- index advice (#18);
- the entity map, entity-key hashing and hotspots (#19–#22);
- full-analysis orchestration (#24);
- evals, model selection and the scale test (#25).

Their tickets carry the `paused` label, without `ready-for-agent`, and stay open to be resumed. No entity key reaches the LLM until the entity map exists, so pausing entity-key hashing (#20) along with it weakens no privacy guarantee.

**One engineer, running locally.** The API binds to `127.0.0.1`, with an optional static token. There are no user accounts. The monthly budget cap (#23) is paused, and usage is shown in the UI instead.

**Stack:** as the proposal recommends.
- FastAPI.
- A Vite + React + TypeScript app in `web/`.
- A TypeScript client generated from the OpenAPI spec.
- Chat streams `AgentEvent`s over SSE. A separate cancel request stops a Turn, and a Thread has at most one active Turn.

**The seams are frozen in the API work, not before it.** The first API ticket shapes `AnalyzerService` for HTTP, for example listing Connections and Threads, and cancelling a Turn. It adds `subagent_started` and `subagent_finished` events to `AgentEvent`. Then it freezes both at v1 in an ADR. #26 is split: its freeze is that ticket, and its new-user docs move to the release.

**Cut from API and UI v1:**
- The entity-map interrupt endpoint and form (M8-04, M9-05). The resume seam returns with the entity map.
- Orchestrator todos in the progress panel (M9-04). The panel shows tools, subagents, SQL statements and Runs instead.

**Added to API and UI v1:**
- Starting deterministic Runs (inventory, workload, targeted tables, exact counts) without chat. They need no model, so Findings, Runs, comparison and export work without an OpenAI key.

**A Connection's DSN is still never stored.** The UI asks for the name of an environment variable, and the API process's environment must hold the DSN. The UI says so when the variable is missing.

**The release keeps its exit items:**
- Playwright e2e without the entity-map step.
- Docker and `pipx` packaging.
- The docs, including #26's.
- A validation run on a real database, which includes the live Azure Query Store check deferred by #17.
- The PG 15–18 test matrix (#27).

## Considered Options

- **Finish the agent first (the original plan):** rejected. The API and UI would wait months on features (hotspots, index advice, evals) that the browser workflow doesn't need to be useful.
- **Freeze the facade before any API work:** rejected. The API's needs would then change a frozen interface straight away.
- **Store DSNs in a local key file so the UI can take one:** deferred. It reverses a rule the code checks in several places, and a `.env` line is a small price for one local engineer.
- **WebSocket for chat:** rejected. Events only flow from server to browser. SSE reconnects and proxies cleanly, and cancelling is one request.
- **HTMX or Streamlit instead of React:** rejected. Both fight the live event stream that the chat and progress panel render.
