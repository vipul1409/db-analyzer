"""AnalyzerService: the single entry point for the CLI, LangGraph Studio, the API and tests."""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import psycopg
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_stream_writer

from db_analyzer import report, runs
from db_analyzer.adapters import postgres as pg
from db_analyzer.adapters.postgres import probe as pg_probe
from db_analyzer.adapters.postgres.queries import LIBRARY
from db_analyzer.adapters.postgres.session import open_session
from db_analyzer.adapters.sql_common.templates import run_template
from db_analyzer.agent import digest
from db_analyzer.agent.events import (
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
    ConnectionRefused,
    Finding,
    FindingStatus,
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
    UnknownCollections,
    WorkloadReport,
)
from db_analyzer.core.run_view import RunView
from db_analyzer.safety.aliases import Aliases, assign
from db_analyzer.safety.executor import QueryBudget, Row, SafeExecutor
from db_analyzer.store.store import Store

CURRENT_STATUSES: tuple[FindingStatus, ...] = ("open", "acknowledged", "fixed")  # not obsolete
SqlObserver = Callable[[AuditEntry], None]


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
        return self._store.findings(connection_id, statuses)

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
        return self._store.observations(connection_id, fingerprint)

    def compare_runs(self, run_a: str, run_b: str) -> RunComparison:
        """What changed between two Runs of one Connection, oldest first, over the scope both
        measured only."""
        a, b = self._store.get_run(run_a), self._store.get_run(run_b)
        if a.connection_id != b.connection_id:
            raise ValueError("Runs of different Connections cannot be compared")
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
        requests = self._store.llm_requests(thread_id)
        costs = [r.cost_usd for r in requests]
        return Usage(
            model=", ".join(sorted({r.model for r in requests})) or self._llm.model,
            input_tokens=sum(r.input_tokens for r in requests),
            cached_tokens=sum(r.cached_tokens for r in requests),
            output_tokens=sum(r.output_tokens for r in requests),
            cost_usd=None if None in costs else sum(c for c in costs if c is not None),
        )

    async def send(self, thread_id: str, message: str) -> AsyncIterator[AgentEvent]:
        """One Turn: stream the agent's events for `message`, ending with Usage and Done."""
        thread = self._store.get_thread(thread_id)
        settings = self._llm
        failed = False
        try:
            aliases = await asyncio.to_thread(self._aliases, thread)
        except (QueryRejected, ConnectionRefused, psycopg.Error) as e:
            yield Error(message=f"cannot list identifiers to alias: {e}")
            yield Done(thread_id=thread.id, answer="", ok=False)
            return
        translator = Translator(aliases)
        config: Any = {"configurable": {"thread_id": thread.id}}
        async with (
            AsyncSqliteSaver.from_conn_string(str(self._home / "checkpoints.sqlite")) as cp,
            httpx.AsyncClient(timeout=settings.request_timeout_s) as http,
        ):
            agent, gateway = self._agent(thread, AgentTurn(self, thread), cp, http, aliases)
            gateway.cassette = _cassette(settings, await _turns_so_far(agent, config))
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
                failed = True
                yield Error(message=f"{type(e).__name__}: {e}")
        for event in translator.flush():
            yield event
        yield gateway.usage()
        yield Done(thread_id=thread.id, answer=translator.answer, ok=not failed)

    def studio_graph(self, connection_name: str) -> Any:
        """The agent for LangGraph Studio, which brings its own checkpointer. One Thread per
        Studio server session records the SQL audit and model requests."""
        thread = self.start_thread(self.connection(connection_name).id)
        turn = AgentTurn(self, thread)
        agent, _ = self._agent(
            thread, turn, None, None, self._aliases(thread), extra=[NewTurn(turn)]
        )
        return agent

    def _agent(
        self,
        thread: Thread,
        turn: "AgentTurn",
        checkpointer: Any,
        http: httpx.AsyncClient | None,
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
        model = make_model(settings, http)
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
            raise ConnectionRefused(f"environment variable {connection.dsn_env} is not set")

        def audit(entry: AuditEntry) -> None:
            self._store.record_audit(entry)
            if on_sql is not None:
                on_sql(entry)

        with open_session(dsn, connection.limits) as conn:
            # No column is a confirmed entity key until entity maps exist.
            yield SafeExecutor(conn, connection.id, audit, connection.gate, budget, thread_id)


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
