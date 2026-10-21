"""The Turn engine: one Turn per Thread, streamed, with cooperative cancellation (ADR 0016).

`AnalyzerService.send` and `.cancel` delegate here. The engine claims the Thread, runs the
agent's stream in a task of its own, and ends every Turn with `usage` then `done` (with
`cancelled`). It drives the Thread's agent through callbacks the service supplies, so history
and Studio keep building the agent the same way, and the analysis facade and this engine can be
read apart."""

import asyncio
import threading
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from db_analyzer.agent.events import AgentEvent, Done, Error, Translator, Usage
from db_analyzer.agent.llm import (
    Cassette,
    CassetteExhausted,
    CassetteMismatch,
    LLMGateway,
    LLMSettings,
    make_model,
)
from db_analyzer.core.model import (
    ConnectionRefused,
    LLMRequestLog,
    QueryRejected,
    Thread,
)
from db_analyzer.safety.aliases import Aliases
from db_analyzer.store.store import Store

# How the service assembles a Thread's agent, shared with history and Studio so all three build
# it the same way. A Turn builds with the default middleware, so this is the no-extra form of
# the service's builder. Returns the agent and the gateway whose usage ends the Turn.
BuildAgent = Callable[[Thread, Any, Any, BaseChatModel, Aliases | None], tuple[Any, LLMGateway]]


class TurnActive(Exception):
    """A Thread already has a Turn running: a second would interleave with it."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"Thread {thread_id} already has a Turn running")
        self.thread_id = thread_id


class TurnEngine:
    """The Turn/streaming/cancellation machinery behind `send`/`cancel`. It owns the per-Thread
    active-Turn registry and the stream/queue plumbing; the agent itself is built by the
    callbacks the service passes in."""

    def __init__(
        self,
        store: Store,
        settings: LLMSettings,
        checkpoints: Path,
        agent_turn: Callable[[Thread], Any],
        build_agent: BuildAgent,
        aliases: Callable[[Thread], Aliases | None],
        usage: Callable[[list[LLMRequestLog]], Usage],
    ) -> None:
        self._store = store
        self._settings = settings
        self._checkpoints = checkpoints
        self._agent_turn = agent_turn
        self._build_agent = build_agent
        self._aliases = aliases
        self._usage = usage
        self._turns = _Turns()

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
        turn = self._run(thread, message, active)
        weakref.finalize(turn, self._turns.release, thread.id, active)
        return turn

    def cancel(self, thread_id: str) -> bool:
        """Cancel the Thread's Turn in progress, if any: its stream stops at the next await
        point and still ends with Usage, then Done with `cancelled`. Returns whether a Turn was
        running (and not already cancelled). Safe to call from any thread."""
        self._store.get_thread(thread_id)
        return self._turns.cancel(thread_id)

    async def _run(
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
        settings = self._settings
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
            agent, state.gateway = self._build_agent(
                thread, self._agent_turn(thread), cp, model, aliases
            )
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
