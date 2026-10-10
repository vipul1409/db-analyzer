"""AnalyzerService: the single entry point for the CLI, LangGraph Studio, the API and tests."""

import asyncio
import os
import threading
import weakref
from collections.abc import AsyncIterator, Callable, Collection, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import psycopg
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_stream_writer

from db_analyzer import report, runs
from db_analyzer.adapters import postgres as pg
from db_analyzer.adapters.postgres import probe as pg_probe
from db_analyzer.adapters.postgres.queries import LIBRARY
from db_analyzer.adapters.postgres.session import open_auxiliary_session, open_session
from db_analyzer.adapters.sql_common.templates import run_template
from db_analyzer.agent import digest
from db_analyzer.agent.events import (
    TURN_LIMIT,
    AgentEvent,
    Done,
    Error,
    LimitReached,
    RunFinished,
    Translator,
    Usage,
    from_audit,
)
from db_analyzer.agent.graph import build_agent
from db_analyzer.agent.llm import (
    Cassette,
    CassetteExhausted,
    CassetteMismatch,
    LLMGateway,
    LLMSettings,
    StrictTools,
    TurnLimits,
    make_model,
)
from db_analyzer.agent.tools import build_tools
from db_analyzer.analyzers import workload
from db_analyzer.core import lifecycle, run_view
from db_analyzer.core.comparison import RunComparison, compare
from db_analyzer.core.model import (
    AuditEntry,
    Capability,
    Connection,
    ConnectionInfo,
    ConnectionRefused,
    DsnEnvMissing,
    Finding,
    FindingStatus,
    FindingView,
    GateLimits,
    LLMRequestLog,
    Observation,
    ProbeResult,
    QueryCapReached,
    QueryRejected,
    Run,
    SessionLimits,
    SettableStatus,
    StorageStats,
    Thread,
    ThreadMessage,
    UnknownCollections,
    WorkloadReport,
)
from db_analyzer.core.run_view import RunView
from db_analyzer.safety.aliases import Aliases, assign
from db_analyzer.safety.executor import QueryBudget, Row, SafeExecutor
from db_analyzer.store.store import Store

CURRENT_STATUSES: tuple[FindingStatus, ...] = ("open", "acknowledged", "fixed")  # not obsolete
SqlObserver = Callable[[AuditEntry], None]


class TurnActive(Exception):
    """A Thread already has a Turn running: a second would interleave with it."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"Thread {thread_id} already has a Turn running")
        self.thread_id = thread_id


class RunsNotComparable(ValueError):
    """Runs of different Connections."""


def default_home() -> Path:
    return Path(os.environ.get("DBX_HOME", Path.home() / ".db-analyzer"))


class AnalyzerService:
    def __init__(
        self,
        home: Path | None = None,
        llm: LLMSettings | None = None,
        min_stats_window: timedelta = workload.MIN_STATS_WINDOW,
    ):
        """`min_stats_window` is the youngest workload statistics the agent's workload Runs
        rank (a deterministic Run takes its own)."""
        self._home = home or default_home()
        self._store = Store(self._home / "db-analyzer.sqlite")
        self._llm = llm or LLMSettings.from_env()
        self._checkpoints = self._home / "checkpoints.sqlite"
        self._turns = _Turns()
        self.min_stats_window = min_stats_window

    def add_connection(
        self,
        name: str,
        dsn_env: str,
        limits: SessionLimits | None = None,
        gate: GateLimits | None = None,
        alias_identifiers: bool | None = None,
    ) -> Connection:
        """Add a Connection, or update the one with this name. `dsn_env` names the env var
        holding the DSN; the DSN itself is never stored. With `alias_identifiers`, the LLM sees
        schema, table and column names only as aliases. Omitting `limits`, `gate` or
        `alias_identifiers` keeps the Connection's existing setting."""
        return self._store.upsert_connection(
            name, "postgres", dsn_env, limits, gate, alias_identifiers
        )

    def connection(self, name: str) -> Connection:
        return self._store.find_connection(name)

    def connections(self) -> list[ConnectionInfo]:
        """Every Connection, by name, each saying whether its DSN variable is set here."""
        return [_info(c) for c in self._store.connections()]

    def connection_info(self, connection_id: str) -> ConnectionInfo:
        return _info(self._store.get_connection(connection_id))

    @property
    def chat_available(self) -> bool:
        """Whether Turns can reach a model: an API key is set, or responses are replayed."""
        return bool(os.environ.get(self._llm.api_key_env)) or self._llm.replaying

    def capabilities(self, connection_id: str) -> frozenset[Capability]:
        self._store.get_connection(connection_id)  # only Postgres today: one adapter
        return pg.CAPABILITIES

    def probe(
        self,
        connection_id: str,
        thread_id: str | None = None,
        budget: QueryBudget | None = None,
        on_sql: SqlObserver | None = None,
    ) -> ProbeResult:
        connection = self._store.get_connection(connection_id)
        with self._executor(connection, thread_id, budget, on_sql) as executor:
            result = pg_probe.probe(executor)
        self._store.save_probe(connection.id, result)
        return result

    def latest_probe(self, connection_id: str) -> ProbeResult | None:
        """The Connection's most recent probe, from the store: the database is not touched.
        None if it was never probed."""
        self._store.get_connection(connection_id)
        return self._store.latest_probe(connection_id)

    def run(
        self,
        connection_id: str,
        analyzers: Sequence[str] = runs.DEFAULT,
        thread_id: str | None = None,
        budget: QueryBudget | None = None,
        on_sql: SqlObserver | None = None,
        *,
        collections: Sequence[str] | None = None,
        exact_counts: bool = False,
        min_stats_window: timedelta = workload.MIN_STATS_WINDOW,
    ) -> Run:
        """A deterministic Run, no LLM: probe, collect with each analyzer, record Findings and
        Observations, then move the Connection's Findings through their lifecycle.

        `inventory` measures sizes and index health. `collections` targets it at those tables
        (schema-qualified, or bare when unambiguous); None measures every one. `exact_counts`
        also counts their rows with count(*), each only where the EXPLAIN gate allows.

        `workload` ranks the most expensive statements, and refuses to when its statistics are
        younger than `min_stats_window`. With no workload source it reviews the schema instead
        (ADR 0010). Either way the Run is partial when no ranking was made.

        Raises `runs.UnknownAnalyzer`, or `runs.OptionsNotAccepted` for an option none of
        `analyzers` accepts."""
        options = runs.RunOptions(collections, exact_counts, min_stats_window)
        chosen = runs.chosen_analyzers(analyzers, options)
        connection = self._store.get_connection(connection_id)
        run = self._store.start_run(connection.id, thread_id)
        try:
            with self._executor(connection, thread_id, budget, on_sql) as executor:
                probe = pg_probe.probe(executor)
                context = runs.RunContext(executor, probe, connection.gate, options)
                outcome = runs.combine({a.name: a.collect(context) for a in chosen})
            self._store.save_probe(connection.id, probe)
            self._store.save_snapshots(run.id, outcome.storage)
            if outcome.workload is not None:
                self._store.save_workload(run.id, outcome.workload)
            self._store.record_observations(connection.id, run.id, outcome.found)
        except BaseException:
            self._store.finish_run(run.id, "failed", scope={}, skipped={})
            raise
        run = self._store.finish_run(
            run.id, outcome.status, scope=outcome.scope, skipped=outcome.skipped
        )
        changed = lifecycle.after_run(
            run,
            self._store.findings(connection.id),
            {o.fingerprint for o in outcome.found},
            existing=outcome.existing,
        )
        self._store.update_findings(changed)
        return run

    def run_sql(
        self,
        connection_id: str,
        sql: str,
        purpose: str,
        thread_id: str | None = None,
        budget: QueryBudget | None = None,
        on_sql: SqlObserver | None = None,
    ) -> list[Row]:
        """Run SQL the LLM wrote: agent guard profile, EXPLAIN gate and privacy filter. Not a
        Run: nothing is measured or recorded beyond the audit log."""
        connection = self._store.get_connection(connection_id)
        with self._executor(connection, thread_id, budget, on_sql) as executor:
            return executor.execute_agent(sql, purpose)

    def runs(self, connection_id: str) -> list[Run]:
        """Oldest first."""
        self._store.get_connection(connection_id)
        return self._store.runs(connection_id)

    def run_observations(self, run_id: str) -> list[Observation]:
        """What a Run observed, in the analyzer's rank order (most severe first)."""
        return self._store.run_observations(run_id)

    def storage(self, run_id: str) -> list[StorageStats]:
        """Collection sizes measured by a Run."""
        return self._store.snapshots(run_id)

    def workload(self, run_id: str) -> WorkloadReport | None:
        """What a Run's workload analyzer found: the ranked statements, how long the statistics
        cover, why nothing was ranked if so. None if the Run did not include it."""
        return self._store.workload(run_id)

    def findings(
        self, connection_id: str, statuses: Collection[FindingStatus] = CURRENT_STATUSES
    ) -> list[Finding]:
        """The Connection's Findings with one of `statuses`; obsolete ones only when asked for.
        A Finding with `unobserved_by` set is asking "fixed?"."""
        self._store.get_connection(connection_id)
        return self._store.findings(connection_id, statuses)

    def finding_views(
        self, connection_id: str, statuses: Collection[FindingStatus] = CURRENT_STATUSES
    ) -> list[FindingView]:
        """`findings`, each with its latest Observation (severity, title, recommendation)."""
        listed = self.findings(connection_id, statuses)
        latest = self._store.latest_observations(connection_id)
        return [FindingView(f, latest[f.fingerprint]) for f in listed]

    def set_finding_status(
        self,
        connection_id: str,
        fingerprint: str,
        status: SettableStatus,
    ) -> Finding:
        """Acknowledge a Finding, confirm it fixed, or reopen it. Settles any "fixed?" prompt.
        Obsolete is set by Runs only, when the subject is gone."""
        if status not in ("open", "acknowledged", "fixed"):
            raise ValueError(f"cannot set a Finding to {status!r}")
        return self._store.set_finding_status(connection_id, fingerprint, status)

    def observations(self, connection_id: str, fingerprint: str) -> list[Observation]:
        """The Finding's Observations, one per Run that saw it, oldest first."""
        observed = self._store.observations(connection_id, fingerprint)
        if not observed:  # every Finding was observed at least once
            raise KeyError(f"no Finding {fingerprint!r}")
        return observed

    def compare_runs(self, run_a: str, run_b: str) -> RunComparison:
        """What changed between two Runs of one Connection, oldest first, over the scope both
        measured only."""
        a, b = self._store.get_run(run_a), self._store.get_run(run_b)
        if a.connection_id != b.connection_id:
            raise RunsNotComparable("Runs of different Connections cannot be compared")
        by_fingerprint = {f.fingerprint: f for f in self._store.findings(a.connection_id)}

        def seen(run: Run) -> list[Finding]:
            return [by_fingerprint[o.fingerprint] for o in self._store.run_observations(run.id)]

        return compare(
            a, b, self._store.snapshots(a.id), self._store.snapshots(b.id), seen(a), seen(b)
        )

    def run_view(self, run_id: str) -> RunView:
        """Everything a reader shows of one Run, ranked, with problems, facts and what was
        skipped already told apart."""
        run = self._store.get_run(run_id)
        return run_view.build(
            self._store.get_connection(run.connection_id),
            run,
            self._store.run_observations(run_id),
            self._store.snapshots(run_id),
            self._store.workload(run_id),
        )

    def export(self, run_id: str, fmt: Literal["md", "json"] = "md") -> bytes:
        """A Run's report: Markdown to read, or JSON laid out so two exports diff line by
        line."""
        view = self.run_view(run_id)
        return (report.json_export(view) if fmt == "json" else report.markdown(view)).encode()

    def audit(self, connection_id: str, thread_id: str | None = None) -> list[AuditEntry]:
        """Every statement executed or rejected on the Connection, oldest first; with
        `thread_id`, only that Thread's."""
        self._store.get_connection(connection_id)
        return self._store.audit(connection_id, thread_id)

    # --- Threads and the agent ------------------------------------------------------------

    def start_thread(self, connection_id: str) -> Thread:
        """A new conversation, bound to this Connection for its whole life."""
        return self._store.create_thread(connection_id)

    def thread(self, thread_id: str) -> Thread:
        return self._store.get_thread(thread_id)

    def llm_requests(self, thread_id: str) -> list[LLMRequestLog]:
        return self._store.llm_requests(thread_id)

    def record_llm_request(self, thread_id: str, entry: LLMRequestLog) -> None:
        self._store.record_llm_request(thread_id, entry)

    def usage(self, thread_id: str) -> Usage:
        """Tokens and estimated cost of every model request in the Thread so far."""
        self._store.get_thread(thread_id)
        return self._usage(self._store.llm_requests(thread_id))

    def usage_in_month(self, month: date | None = None) -> Usage:
        """Tokens and estimated cost across every Thread in the calendar month (UTC) holding
        `month`, by default the current one. Cost is None when any request's cost is unknown."""
        first = (month or datetime.now(UTC).date()).replace(day=1)
        after = (first + timedelta(days=32)).replace(day=1)
        start, end = (datetime.combine(d, time(), UTC) for d in (first, after))
        return self._usage(self._store.llm_requests_between(start, end))

    def _usage(self, requests: list[LLMRequestLog]) -> Usage:
        costs = [r.cost_usd for r in requests]
        return Usage(
            model=", ".join(sorted({r.model for r in requests})) or self._llm.model,
            input_tokens=sum(r.input_tokens for r in requests),
            cached_tokens=sum(r.cached_tokens for r in requests),
            output_tokens=sum(r.output_tokens for r in requests),
            cost_usd=None if None in costs else sum(c for c in costs if c is not None),
        )

    def threads(self, connection_id: str) -> list[Thread]:
        """The Connection's Threads, latest activity first."""
        self._store.get_connection(connection_id)
        return self._store.threads(connection_id)

    async def history(self, thread_id: str) -> list[ThreadMessage]:
        """The Thread's messages and the agent's final answers, in order, from its checkpoint.
        Tool calls and SQL are not replayed: the audit log holds them. Needs no model key."""
        thread = self._store.get_thread(thread_id)
        known = self._store.aliases(thread.connection_id)
        aliases = Aliases(known) if known else None
        async with AsyncSqliteSaver.from_conn_string(str(self._checkpoints)) as cp:
            model = make_model(self._llm, offline=True)
            agent, _ = self._agent(thread, AgentTurn(self, thread), cp, model, aliases)
            state = await agent.aget_state({"configurable": {"thread_id": thread.id}})
        messages = state.values.get("messages", [])
        return [h for m in messages if (h := _history_message(m, aliases))]

    def send(self, thread_id: str, message: str) -> AsyncIterator[AgentEvent]:
        """One Turn: stream the agent's events for `message`, ending with Usage and Done.

        A Thread has at most one Turn at a time: the Turn is claimed now, so this raises
        TurnActive (and KeyError for an unknown Thread) before anything streams. Iterate the
        result to run the Turn; dropping it unread releases the Thread."""
        thread = self._store.get_thread(thread_id)
        active = self._turns.claim(thread.id)
        try:
            self._store.turn_started(thread.id, message)
        except BaseException:
            self._turns.release(thread.id, active)
            raise
        turn = self._turn(thread, message, active)
        weakref.finalize(turn, self._turns.release, thread.id, active)
        return turn

    def cancel(self, thread_id: str) -> bool:
        """Cancel the Thread's Turn in progress, if any: its stream stops at the next await
        point and still ends with Usage, then Done with `cancelled`. Returns whether a Turn was
        running (and not already cancelled). Safe to call from any thread."""
        self._store.get_thread(thread_id)
        return self._turns.cancel(thread_id)

    async def _turn(
        self, thread: Thread, message: str, active: "_ActiveTurn"
    ) -> AsyncIterator[AgentEvent]:
        state = _TurnState()
        try:
            stop = active.start()
            async for event in _until_set(stop, self._agent_events(thread, message, state)):
                yield event
                if isinstance(event, Done):  # the Turn ended before reaching the model
                    return
            for event in state.translator.flush():
                yield event
            yield state.gateway.usage() if state.gateway else self._usage([])
            yield Done(
                thread_id=thread.id,
                answer=state.translator.answer,
                ok=not (state.failed or active.cancelled),
                cancelled=active.cancelled,
            )
        finally:
            self._turns.release(thread.id, active)

    async def _agent_events(
        self, thread: Thread, message: str, state: "_TurnState"
    ) -> AsyncIterator[AgentEvent]:
        """The agent's events for one Turn. What ends the Turn (the remaining translated text,
        usage, Done) is left to the caller, which may have cancelled it; `state` carries what it
        needs."""
        settings = self._llm
        try:
            aliases = await asyncio.to_thread(self._aliases, thread)
        except (QueryRejected, ConnectionRefused, psycopg.Error) as e:
            yield Error(message=f"cannot list identifiers to alias: {e}")
            yield Done(thread_id=thread.id, answer="", ok=False)
            return
        state.translator = translator = Translator(aliases)
        config: Any = {"configurable": {"thread_id": thread.id}}
        async with (
            AsyncSqliteSaver.from_conn_string(str(self._checkpoints)) as cp,
            httpx.AsyncClient(timeout=settings.request_timeout_s) as http,
        ):
            model = make_model(settings, http)
            agent, state.gateway = self._agent(thread, AgentTurn(self, thread), cp, model, aliases)
            state.gateway.cassette = _cassette(settings, await _turns_so_far(agent, config))
            try:
                async for namespace, mode, chunk in agent.astream(
                    {"messages": [HumanMessage(content=message)]},
                    config,
                    stream_mode=["messages", "updates", "custom"],
                    subgraphs=True,  # a subagent's tool and SQL events stream only this way
                ):
                    for event in translator.translate(mode, chunk, namespace):
                        yield event
            except (CassetteExhausted, CassetteMismatch):
                raise  # a stale recording must fail the test, not become an event
            except Exception as e:  # the Turn ends; the Thread stays usable
                state.failed = True
                yield Error(message=f"{type(e).__name__}: {e}")

    def studio_graph(self, connection_name: str) -> Any:
        """The agent for LangGraph Studio, which brings its own checkpointer. One Thread per
        Studio server session records the SQL audit and model requests."""
        thread = self.start_thread(self.connection(connection_name).id)
        turn = AgentTurn(self, thread)
        model = make_model(self._llm)
        agent, _ = self._agent(
            thread, turn, None, model, self._aliases(thread), extra=[NewTurn(turn)]
        )
        return agent

    def _agent(
        self,
        thread: Thread,
        turn: "AgentTurn",
        checkpointer: Any,
        model: BaseChatModel,
        aliases: Aliases | None,
        extra: Sequence[Any] = (),
    ) -> tuple[Any, LLMGateway]:
        settings = self._llm
        gateway = LLMGateway(
            settings,
            log=lambda entry: self.record_llm_request(thread.id, entry),
            aliases=aliases,
        )
        middleware = [
            TurnLimits(settings.max_tokens_per_turn, settings.max_tool_calls_per_turn),
            StrictTools(),
            gateway,  # shared with subagents: one concurrency cap, log and cassette per Turn
        ]
        tools = build_tools(turn, self.capabilities(thread.connection_id))
        return build_agent(model, tools, middleware, checkpointer, extra), gateway

    def _aliases(self, thread: Thread) -> Aliases | None:
        """The Connection's identifier aliases, extended to names added since the last Turn,
        or None when it does not alias identifiers."""
        connection = self._store.get_connection(thread.connection_id)
        if not connection.alias_identifiers:
            return None
        with self._executor(connection, thread.id) as executor:
            version = executor.server_version_num
            rows = run_template(executor, LIBRARY.get("identifiers", version), "aliases")
        known = self._store.aliases(connection.id)
        new = assign(known, [(str(r["kind"]), str(r["name"])) for r in rows])
        self._store.add_aliases(connection.id, new)
        return Aliases({**known, **new})

    @contextmanager
    def _executor(
        self,
        connection: Connection,
        thread_id: str | None = None,
        budget: QueryBudget | None = None,
        on_sql: SqlObserver | None = None,
    ) -> Iterator[SafeExecutor]:
        dsn = os.environ.get(connection.dsn_env)
        if not dsn:
            raise DsnEnvMissing(connection.dsn_env)

        def audit(entry: AuditEntry) -> None:
            self._store.record_audit(entry)
            if on_sql is not None:
                on_sql(entry)

        def auxiliary(database: str) -> psycopg.Connection[Any]:
            return open_auxiliary_session(dsn, database, connection.limits)

        with open_session(dsn, connection.limits) as conn:
            # No column is a confirmed entity key until entity maps exist.
            yield SafeExecutor(
                conn,
                connection.id,
                audit,
                connection.gate,
                budget,
                thread_id,
                open_auxiliary=auxiliary,
            )


class AgentTurn:
    """The TurnBackend the agent's tools call: one Thread's Connection, one Turn's query cap,
    and SQL and Run events streamed to the caller. Failures come back as explicit errors, so
    the model reports them instead of filling the gap (ADR 0003)."""

    def __init__(self, service: AnalyzerService, thread: Thread):
        self._service = service
        self._thread = thread
        self._budget = QueryBudget()

    def new_turn(self) -> None:
        """For hosts that reuse one backend across Turns (LangGraph Studio)."""
        self._budget.reset()

    def probe(self) -> dict[str, Any]:
        return self._guarded(
            lambda emit: digest.probe(
                self._service.probe(
                    self._thread.connection_id, self._thread.id, self._budget, _sql_events(emit)
                )
            )
        )

    def storage(self, top_n: int) -> dict[str, Any]:
        def measure(emit: Callable[[Any], None]) -> dict[str, Any]:
            run = self._service.run(
                self._thread.connection_id,
                ["inventory"],
                self._thread.id,
                self._budget,
                _sql_events(emit),
            )
            emit(RunFinished(run_id=run.id, status=run.status).model_dump())
            return digest.storage(self._service.run_view(run.id), top_n)

        return self._guarded(measure)

    def count_exact(self, tables: list[str], all_tables: bool) -> dict[str, Any]:
        if not tables and not all_tables:
            return {"error": "name the tables to count, or set all_tables"}

        def count(emit: Callable[[Any], None]) -> dict[str, Any]:
            run = self._service.run(
                self._thread.connection_id,
                ["inventory"],
                self._thread.id,
                self._budget,
                _sql_events(emit),
                collections=None if all_tables else tables,
                exact_counts=True,
            )
            emit(RunFinished(run_id=run.id, status=run.status).model_dump())
            return digest.exact_counts(self._service.run_view(run.id))

        return self._guarded(count)

    def sql(self, sql: str, purpose: str) -> dict[str, Any]:
        return self._guarded(
            lambda emit: digest.sql_result(
                self._service.run_sql(
                    self._thread.connection_id,
                    sql,
                    purpose,
                    self._thread.id,
                    self._budget,
                    _sql_events(emit),
                )
            )
        )

    def top_queries(self, top_n: int) -> dict[str, Any]:
        def rank(emit: Callable[[Any], None]) -> dict[str, Any]:
            run = self._service.run(
                self._thread.connection_id,
                ["workload"],
                self._thread.id,
                self._budget,
                _sql_events(emit),
                min_stats_window=self._service.min_stats_window,
            )
            emit(RunFinished(run_id=run.id, status=run.status).model_dump())
            return digest.top_queries(self._service.run_view(run.id), top_n)

        return self._guarded(rank)

    def query_details(self, rank: int) -> dict[str, Any]:
        """From the Thread's latest workload Run, else the Connection's: no SQL, no Run."""
        run = self._latest_workload_run()
        if run is None:
            return {"error": "no workload Run yet: call get_top_queries first"}
        return digest.query_details(self._service.run_view(run.id), rank)

    def _latest_workload_run(self) -> Run | None:
        connections: Run | None = None
        for r in reversed(self._service.runs(self._thread.connection_id)):
            ours = r.thread_id == self._thread.id
            if (ours or connections is None) and self._service.workload(r.id) is not None:
                if ours:
                    return r
                connections = r
        return connections

    def _guarded(self, work: Callable[[Callable[[Any], None]], dict[str, Any]]) -> dict[str, Any]:
        emit = _stream_writer()
        try:
            return work(emit)
        except QueryCapReached as e:
            used = self._budget.used
            emit(LimitReached(limit="queries", used=used, max=e.limit).model_dump())
            return {"error": e.reason}
        except (QueryRejected, ConnectionRefused, UnknownCollections) as e:
            return {"error": str(e)}
        except psycopg.Error as e:
            return {"error": f"database error: {str(e).strip()}"}


class NewTurn(AgentMiddleware[Any, Any, Any]):
    """Gives a reused AgentTurn a fresh query cap at the start of each agent invocation.
    Studio runs that overlap in time share the cap: acceptable for a development tool."""

    def __init__(self, turn: AgentTurn):
        super().__init__()
        self._turn = turn

    def before_agent(self, state: Any, runtime: Any) -> None:
        self._turn.new_turn()


@dataclass
class _TurnState:
    """What ending a Turn needs from the part that ran the agent."""

    translator: Translator = field(default_factory=Translator)
    gateway: LLMGateway | None = None
    failed: bool = False


class _ActiveTurn:
    """A claimed Turn. Cancelling it sets an event on the loop that runs it, from any thread."""

    def __init__(self) -> None:
        self.cancelled = False
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None

    def start(self) -> asyncio.Event:
        """On the loop that runs the Turn: the event that stops it."""
        with self._lock:
            self._loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
            if self.cancelled:
                self._stop.set()
            return self._stop

    def cancel(self) -> bool:
        with self._lock:
            if self.cancelled:
                return False
            self.cancelled = True
            if self._loop is not None and self._stop is not None:
                self._loop.call_soon_threadsafe(self._stop.set)
            return True


class _Turns:
    """The Turn in progress in each Thread of this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, _ActiveTurn] = {}

    def claim(self, thread_id: str) -> _ActiveTurn:
        with self._lock:
            if thread_id in self._active:
                raise TurnActive(thread_id)
            turn = self._active[thread_id] = _ActiveTurn()
            return turn

    def release(self, thread_id: str, turn: _ActiveTurn) -> None:
        with self._lock:
            if self._active.get(thread_id) is turn:
                del self._active[thread_id]

    def cancel(self, thread_id: str) -> bool:
        with self._lock:
            turn = self._active.get(thread_id)
        return turn is not None and turn.cancel()


_END = object()


async def _until_set[T](stop: asyncio.Event, events: AsyncIterator[T]) -> AsyncIterator[T]:
    """`events` until `stop` is set. They are produced in a task of their own, which is
    cancelled when `stop` is set or the consumer stops, so the producer is interrupted at its
    next await point."""
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for event in events:
                queue.put_nowait(event)
            queue.put_nowait(_END)
        except Exception as e:
            queue.put_nowait(e)

    producer = asyncio.create_task(produce())
    stopping = asyncio.create_task(stop.wait())
    try:
        while True:
            getting = asyncio.create_task(queue.get())
            await asyncio.wait({getting, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if not getting.done():
                getting.cancel()
                return
            item = getting.result()
            if item is _END:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stopping.cancel()
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer


def _history_message(m: BaseMessage, aliases: Aliases | None) -> ThreadMessage | None:
    """The user's messages as typed; the agent's final answers with real names, as they were
    shown. Preambles to tool calls and turn-limit notices are left out."""
    if isinstance(m, HumanMessage):
        return ThreadMessage("user", m.text)
    if isinstance(m, AIMessage) and not m.tool_calls and m.name != TURN_LIMIT and m.text:
        return ThreadMessage("agent", aliases.unalias(m.text) if aliases else m.text)
    return None


def _info(c: Connection) -> ConnectionInfo:
    return ConnectionInfo(**vars(c), dsn_env_set=bool(os.environ.get(c.dsn_env)))


def _stream_writer() -> Callable[[Any], None]:
    """LangGraph's stream writer inside a graph; outside one (a backend called directly),
    events go nowhere."""
    try:
        return get_stream_writer()
    except RuntimeError:
        return lambda _: None


def _sql_events(emit: Callable[[Any], None]) -> SqlObserver:
    return lambda entry: emit(from_audit(entry).model_dump())


async def _turns_so_far(agent: Any, config: Any) -> int:
    # The graph's state, not the latest checkpoint: a checkpoint stores only changed channels.
    state = await agent.aget_state(config)
    return sum(isinstance(m, HumanMessage) for m in state.values.get("messages", []))


def _cassette(settings: LLMSettings, turn: int) -> Cassette | None:
    """One cassette file per Turn, numbered within the Thread, so a resumed Thread replays the
    right responses after a restart."""
    if settings.cassette is None:
        return None
    return Cassette(settings.cassette / f"turn-{turn}.json", record=settings.record)
