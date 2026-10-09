"""LLM access: the model factory and the middleware every model call goes through.

- `make_model`: OpenAI via LangChain's `init_chat_model`, Responses API, no data retention
  (`store=False`), request timeout and retries with exponential backoff from the SDK.
- `LLMGateway`: the only path to the model. Caps concurrent calls, writes a redacted request
  log, totals usage per turn, and records or replays cassettes so tests run offline.
- `StrictTools`: forces strict JSON schemas on every tool bind (ADR 0003).
- `TurnLimits`: ends a turn cleanly at the token or tool-call limit.
"""

import asyncio
import json
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ToolCallRequest, hook_config
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)
from langgraph.types import Command

from db_analyzer.agent.events import TURN_LIMIT, LimitKind, Usage
from db_analyzer.core.model import LLMRequestLog

DEFAULT_MODEL = "gpt-5.4-mini"

# USD per 1M tokens: (input, cached input, output). ADR 0003, 9 Oct 2026.
PRICES = {
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
}


def estimate_cost(model: str, input_tokens: int, cached: int, output_tokens: int) -> float | None:
    price = next((p for name, p in PRICES.items() if model.startswith(name)), None)
    if price is None:
        return None
    p_in, p_cached, p_out = price
    return ((input_tokens - cached) * p_in + cached * p_cached + output_tokens * p_out) / 1e6


class LLMConfigError(Exception):
    """The model cannot be used as configured (e.g. no API key)."""


@dataclass(frozen=True)
class LLMSettings:
    model: str = DEFAULT_MODEL
    api_key_env: str = "OPENAI_API_KEY"
    request_timeout_s: float = 120
    max_retries: int = 4
    max_concurrency: int = 4
    max_tokens_per_turn: int = 200_000
    max_tool_calls_per_turn: int = 60
    cassette: Path | None = None  # record or replay model responses here
    record: bool = False

    @classmethod
    def from_env(cls) -> "LLMSettings":
        cassette = os.environ.get("DBX_LLM_CASSETTE")
        return cls(
            model=os.environ.get("DBX_MODEL", DEFAULT_MODEL),
            cassette=Path(cassette) if cassette else None,
            record=os.environ.get("DBX_LLM_CASSETTE_MODE") == "record",
        )

    @property
    def replaying(self) -> bool:
        return self.cassette is not None and not self.record


def make_model(
    settings: LLMSettings, http_client: httpx.AsyncClient | None = None
) -> BaseChatModel:
    """Pass an `http_client` owned by the caller's event loop: langchain-openai otherwise
    shares one async client across loops, and every turn after the first fails with
    "Event loop is closed"."""
    api_key = os.environ.get(settings.api_key_env)
    if not api_key:
        if not settings.replaying:
            raise LLMConfigError(f"environment variable {settings.api_key_env} is not set")
        api_key = "replay-only"  # never sent: the gateway answers from the cassette
    return init_chat_model(
        f"openai:{settings.model}",
        api_key=api_key,
        use_responses_api=True,
        store=False,
        timeout=settings.request_timeout_s,
        max_retries=settings.max_retries,
        http_async_client=http_client,
    )


# --- Cassettes ------------------------------------------------------------------------------


class CassetteExhausted(Exception):
    """Replay asked for more model responses than were recorded: re-record the cassette."""


class CassetteMismatch(Exception):
    """The request differs from the recorded one (prompt, tools or flow changed): re-record."""


class Cassette:
    """Model responses in call order, each with the shape of the request that produced it.
    Replay checks the shape and returns the response without calling the model; tool results
    stay live, so tools and the database are exercised for real."""

    def __init__(self, path: Path, record: bool):
        self.path = path
        self.record = record
        self._calls: list[dict[str, Any]] = [] if record else json.loads(path.read_text())["calls"]
        self._next = 0

    def replay(self, request: dict[str, Any]) -> AIMessage:
        if self._next >= len(self._calls):
            raise CassetteExhausted(f"{self.path}: only {len(self._calls)} model calls recorded")
        call = self._calls[self._next]
        if diff := {
            k: (call["request"][k], v) for k, v in request.items() if call["request"][k] != v
        }:
            raise CassetteMismatch(f"{self.path} call {self._next}: recorded vs now {diff}")
        [msg] = messages_from_dict([call["response"]])
        self._next += 1
        assert isinstance(msg, AIMessage)
        return msg

    def add(self, request: dict[str, Any], msg: AIMessage) -> None:
        self._calls.append({"request": request, "response": message_to_dict(msg)})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"calls": self._calls}, indent=1) + "\n")


def _request_shape(request: ModelRequest[Any]) -> dict[str, Any]:
    """What must match on replay: deterministic for a given prompt, toolset and recording."""
    return {
        "tools": sorted(t["name"] if isinstance(t, dict) else t.name for t in request.tools),
        "messages": len(request.messages),
        "last": request.messages[-1].type if request.messages else None,
    }


# --- Gateway --------------------------------------------------------------------------------

_SECRETS = [
    (re.compile(r"(\w+://[^:/@\s]+:)[^@\s]+@"), r"\1***@"),  # DSN passwords
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-***"),  # API keys
]
REQUEST_LOG_CHARS = 4000


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


@dataclass
class _Totals:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0
    models: set[str] = field(default_factory=set)


class LLMGateway(AgentMiddleware[Any, Any, Any]):
    """One instance per turn; share it with subagents so they share its concurrency cap."""

    def __init__(
        self,
        settings: LLMSettings,
        log: Callable[[LLMRequestLog], None] | None = None,
        cassette: Cassette | None = None,
    ):
        super().__init__()
        self._settings = settings
        self._log = log
        self.cassette = cassette  # set before the first model call
        self._async_slots = asyncio.Semaphore(settings.max_concurrency)
        self._slots = threading.BoundedSemaphore(settings.max_concurrency)
        self._totals = _Totals()
        self._lock = threading.Lock()

    def wrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Any]
    ) -> Any:
        started = time.perf_counter()
        with self._slots:
            response = self._replayed(request) or handler(request)
        return self._after(request, response, started)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        started = time.perf_counter()
        async with self._async_slots:
            response = self._replayed(request) or await handler(request)
        # The request log and cassette are file and SQLite writes: keep them off the loop.
        return await asyncio.to_thread(self._after, request, response, started)

    def usage(self) -> Usage:
        t = self._totals
        return Usage(
            model=", ".join(sorted(t.models)) or self._settings.model,
            input_tokens=t.input_tokens,
            cached_tokens=t.cached_tokens,
            output_tokens=t.output_tokens,
            cost_usd=t.cost_usd,
        )

    def _replayed(self, request: ModelRequest[Any]) -> ModelResponse[Any] | None:
        if self.cassette is None or self.cassette.record:
            return None
        return ModelResponse(result=[self.cassette.replay(_request_shape(request))])

    def _after(self, request: ModelRequest[Any], response: Any, started: float) -> Any:
        msg = _ai_message(response)
        if self.cassette is not None and self.cassette.record and msg is not None:
            self.cassette.add(_request_shape(request), msg)
        if msg is not None:
            with self._lock:  # model calls can finish in parallel (subagents)
                self._account(request, msg, started)
        return response

    def _account(self, request: ModelRequest[Any], msg: AIMessage, started: float) -> None:
        model = str(msg.response_metadata.get("model_name") or self._settings.model)
        u = msg.usage_metadata or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        cached = (u.get("input_token_details") or {}).get("cache_read", 0)
        t = self._totals
        t.models.add(model)
        t.input_tokens += u["input_tokens"]
        t.cached_tokens += cached
        t.output_tokens += u["output_tokens"]
        cost = estimate_cost(model, u["input_tokens"], cached, u["output_tokens"])
        # An unknown model gives no estimate rather than a wrong one.
        t.cost_usd = None if cost is None or t.cost_usd is None else t.cost_usd + cost
        if self._log is not None:
            self._log(
                LLMRequestLog(
                    model=model,
                    at=datetime.now(UTC),
                    duration_ms=(time.perf_counter() - started) * 1000,
                    request=_describe(request.messages, request.tools),
                    response=redact(json.dumps(message_to_dict(msg), default=str))[
                        :REQUEST_LOG_CHARS
                    ],
                    input_tokens=u["input_tokens"],
                    cached_tokens=cached,
                    output_tokens=u["output_tokens"],
                    cost_usd=cost,
                )
            )


def _ai_message(response: Any) -> AIMessage | None:
    if isinstance(response, AIMessage):
        return response
    result = getattr(response, "result", None) or []
    return next((m for m in result if isinstance(m, AIMessage)), None)


def _describe(messages: Sequence[BaseMessage], tools: Sequence[Any]) -> str:
    lines = [f"{m.type}: {m.text}" for m in messages]
    names = [t["name"] if isinstance(t, dict) else t.name for t in tools]
    text = "\n".join([f"tools: {', '.join(names)}", *lines])
    return redact(text)[-REQUEST_LOG_CHARS:]


# --- Strict tool schemas --------------------------------------------------------------------


class StrictTools(AgentMiddleware[Any, Any, Any]):
    """create_deep_agent has no `strict` option and the Responses API leaves it off (ADR 0003)."""

    def wrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Any]
    ) -> Any:
        return handler(_strict(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        return await handler(_strict(request))


def _strict(request: ModelRequest[Any]) -> ModelRequest[Any]:
    return request.override(model_settings={**request.model_settings, "strict": True})


# --- Turn limits ----------------------------------------------------------------------------


class TurnLimits(AgentMiddleware[Any, Any, Any]):
    """Counts this turn's tokens and tool calls from the messages since the user's last
    message, so the counts survive checkpoints. A tool call over the limit is not run; the
    turn then ends before the next model call with a TURN_LIMIT message."""

    def __init__(self, max_tokens: int, max_tool_calls: int):
        super().__init__()
        self.max_tokens = max_tokens
        self.max_tool_calls = max_tool_calls

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        turn = _this_turn(state["messages"])
        tokens = _tokens(turn)
        calls = sum(len(m.tool_calls) for m in _ai(turn))
        if tokens >= self.max_tokens:
            return _stop("tokens", tokens, self.max_tokens)
        if calls > self.max_tool_calls:
            return _stop("tool_calls", calls, self.max_tool_calls)
        return None

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """A reply that crosses the token limit ends the turn before its tool calls run. The
        unanswered calls are patched by deepagents at the start of the next turn."""
        tokens = _tokens(_this_turn(state["messages"]))
        return _stop("tokens", tokens, self.max_tokens) if tokens >= self.max_tokens else None

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]
    ) -> ToolMessage | Command[Any]:
        return self._refused(request) or handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]
    ) -> ToolMessage | Command[Any]:
        refused = self._refused(request)
        return refused if refused is not None else await handler(request)

    def _refused(self, request: ToolCallRequest) -> ToolMessage | None:
        call_ids = [
            c["id"] for m in _ai(_this_turn(request.state["messages"])) for c in m.tool_calls
        ]
        position = (
            call_ids.index(request.tool_call["id"]) if request.tool_call["id"] in call_ids else 0
        )
        if position < self.max_tool_calls:
            return None
        return ToolMessage(
            content=f"Not run: the tool-call limit ({self.max_tool_calls} per turn) is reached.",
            tool_call_id=request.tool_call["id"] or "",
            name=request.tool_call["name"],
            status="error",
        )


def _this_turn(messages: list[BaseMessage]) -> list[BaseMessage]:
    last_user = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    return messages[last_user + 1 :]


def _ai(messages: list[BaseMessage]) -> list[AIMessage]:
    return [m for m in messages if isinstance(m, AIMessage)]


def _tokens(turn: list[BaseMessage]) -> int:
    return sum(m.usage_metadata["total_tokens"] for m in _ai(turn) if m.usage_metadata)


def _stop(limit: LimitKind, used: int, maximum: int) -> dict[str, Any]:
    label = limit.replace("_", "-").removesuffix("s")
    msg = AIMessage(
        content=f"Stopped: this turn reached its {label} limit ({used} of {maximum}).",
        name=TURN_LIMIT,
        response_metadata={"limit": limit, "used": used, "max": maximum},
    )
    return {"jump_to": "end", "messages": [msg]}
