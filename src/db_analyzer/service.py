"""AnalyzerService: the single entry point for the CLI, LangGraph Studio, the API and tests."""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import httpx
import psycopg
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_stream_writer

from db_analyzer import report
from db_analyzer.adapters import postgres as pg
from db_analyzer.adapters.postgres import inventory as pg_inventory
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
from db_analyzer.analyzers import index_health, inventory
from db_analyzer.core import lifecycle
from db_analyzer.core.comparison import RunComparison, compare
from db_analyzer.core.model import (
    AnalyzerName,
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
)
from db_analyzer.safety.aliases import Aliases, assign
from db_analyzer.safety.executor import QueryBudget, Row, SafeExecutor
from db_analyzer.store.store import Store

SUPPORTED_ANALYZERS: tuple[AnalyzerName, ...] = ("inventory",)
CURRENT_STATUSES: tuple[FindingStatus, ...] = ("open", "acknowledged", "fixed")  # not obsolete
SqlObserver = Callable[[AuditEntry], None]


def default_home() -> Path:
    return Path(os.environ.get("DBX_HOME", Path.home() / ".db-analyzer"))


class AnalyzerService:
    def __init__(self, home: Path | None = None, llm: LLMSettings | None = None):
        self._home = home or default_home()
        self._store = Store(self._home / "db-analyzer.sqlite")
        self._llm = llm or LLMSettings.from_env()

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
        analyzers: Sequence[AnalyzerName] = SUPPORTED_ANALYZERS,
        thread_id: str | None = None,
        budget: QueryBudget | None = None,
        on_sql: SqlObserver | None = None,
        *,
        collections: Sequence[str] | None = None,
        exact_counts: bool = False,
    ) -> Run:
        """A deterministic Run, no LLM: probe, measure, record Findings and Observations.

        `collections` targets the Run at those tables (schema-qualified, or bare when
        unambiguous); None measures every one. `exact_counts` also counts their rows with
        count(*), each only where the EXPLAIN gate allows: a refused count is recorded on the
        collection with its reason, and the collection keeps its estimate."""
        if unsupported := set(analyzers) - set(SUPPORTED_ANALYZERS):
            raise ValueError(f"analyzers not available yet: {sorted(unsupported)}")
        connection = self._store.get_connection(connection_id)
        run = self._store.start_run(connection.id, thread_id)
        try:
            with self._executor(connection, thread_id, budget, on_sql) as executor:
                probe = pg_probe.probe(executor)
                listed = pg_inventory.storage_stats(executor, probe.server_version_num)
                indexes = pg_inventory.index_stats(executor, probe.server_version_num)
                measured = listed
                if collections is not None:
                    measured = inventory.select(measured, list(collections))
                measured = _measure_table_data(
                    executor, probe, connection.gate, measured, exact_counts
                )
            self._store.save_probe(connection.id, probe)
            self._store.save_snapshots(run.id, measured)
            measured_refs = {m.ref for m in measured}
            index_problems = index_health.analyze(
                [i for i in indexes if i.table in measured_refs],
                stats_reset=probe.stats.database_stats_reset,
                now=probe.taken_at,
                on_replica=probe.in_recovery,
            )
            found = inventory.analyze(
                measured, broad=collections is None, other_problems=index_problems
            )
            self._store.record_observations(connection.id, run.id, found)
        except BaseException:
            self._store.finish_run(run.id, "failed", scope={}, skipped={})
            raise
        # Inventory reads sizes from the catalog, which needs no table privilege and passes the
        # gate: it skips measurements (ADR 0007), never a whole collection, so is never partial.
        run = self._store.finish_run(
            run.id, "complete", scope={"inventory": [m.ref for m in measured]}, skipped={}
        )
        changed = lifecycle.after_run(
            run,
            self._store.findings(connection.id),
            {o.fingerprint for o in found},
            existing={s.ref.qualified for s in listed} | {i.name for i in indexes},
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

    def export(self, run_id: str, fmt: Literal["md", "json"] = "md") -> bytes:
        """A Run's report: Markdown to read, or JSON laid out so two exports diff line by
        line."""
        run = self._store.get_run(run_id)
        connection = self._store.get_connection(run.connection_id)
        measured = self._store.snapshots(run_id)
        observations = self._store.run_observations(run_id)
        if fmt == "json":
            return report.json_export(connection, run, measured, observations).encode()
        return report.markdown(connection, run, measured, observations).encode()

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


def _measure_table_data(
    executor: SafeExecutor,
    probe: ProbeResult,
    gate: GateLimits,
    measured: list[StorageStats],
    exact_counts: bool,
) -> list[StorageStats]:
    """The measurements that read table data rather than the catalog: a dead-tuple scan where
    the counters suggest bloat, and exact counts when asked. Each is skipped, with the reason,
    where the gate or a privilege refuses it; the query cap skips everything after it."""
    out: dict[int, StorageStats] = {i: s for i, s in enumerate(measured)}
    pgstattuple = probe.extension_schemas.get("pgstattuple")
    jobs: list[tuple[int, str]] = [
        (i, "dead_tuple_scan") for i, s in out.items() if inventory.needs_dead_tuple_scan(s)
    ]
    if exact_counts:  # smallest first, so a cap or timeout costs the fewest counts
        jobs += [(i, "exact_count") for i in sorted(out, key=lambda i: out[i].total_bytes)]

    def skip(i: int, job: str, why: str) -> None:
        out[i] = replace(out[i], skipped={**out[i].skipped, job: why})

    capped: str | None = None
    for i, job in jobs:
        s = out[i]
        if capped is not None:
            skip(i, job, capped)
            continue
        try:
            if job == "exact_count":
                n = pg_inventory.count_exactly(executor, s.ref)
                out[i] = replace(s, row_count=n, row_count_method="exact")
            elif pgstattuple is None:
                skip(i, job, "pgstattuple is not installed")
            elif (rows := _rows_to_read(s)) > gate.max_scan_rows:
                skip(
                    i,
                    job,
                    f"about {rows:,} rows to read exceeds the scan limit ({gate.max_scan_rows:,})",
                )
            else:
                out[i] = replace(
                    s, dead_tuple_scan=pg_inventory.scan_dead_tuples(executor, s.ref, pgstattuple)
                )
        except QueryCapReached as e:
            capped = e.reason
            skip(i, job, capped)
        except QueryRejected as e:
            skip(i, job, e.reason)
        except psycopg.errors.InsufficientPrivilege:
            skip(i, job, "no SELECT privilege on the table")
        except psycopg.Error as e:
            skip(i, job, str(e).strip())
    return list(out.values())


def _rows_to_read(s: StorageStats) -> int:
    m = s.maintenance
    return 0 if m is None else m.live_rows + m.dead_rows


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
            return digest.storage(
                run, self._service.storage(run.id), self._service.run_observations(run.id), top_n
            )

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
            return digest.exact_counts(run, self._service.storage(run.id))

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

    def _guarded(self, work: Callable[[Callable[[Any], None]], dict[str, Any]]) -> dict[str, Any]:
        emit = get_stream_writer()
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
