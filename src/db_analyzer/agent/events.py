"""AgentEvent: the typed stream every front end renders (CLI now, API as SSE and UI later).

`Translator` turns LangGraph's stream (`stream_mode=["messages", "updates", "custom"]`) into
events. Tools emit SQL and Run events themselves through LangGraph's stream writer, as plain
dicts of these same models.
"""

import re
from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from db_analyzer.core.model import AuditEntry
from db_analyzer.safety.aliases import Aliases

# Name of the AIMessage the turn-limit middleware adds when it ends a turn.
TURN_LIMIT = "turn_limit"
# The orchestrator's tool that delegates to a subagent (deepagents), and its subagent argument.
DELEGATE, SUBAGENT_ARG = "task", "subagent_type"


class Event(BaseModel):
    """Every field of an event is always sent, defaults included: their schemas say so."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Token(Event):
    type: Literal["token"] = "token"
    text: str


class ToolStarted(Event):
    type: Literal["tool_started"] = "tool_started"
    call_id: str
    name: str
    args: dict[str, Any]


class ToolFinished(Event):
    type: Literal["tool_finished"] = "tool_finished"
    call_id: str
    name: str
    ok: bool
    summary: str


class SubagentStarted(Event):
    """The orchestrator delegated to a subagent through tool call `call_id`."""

    type: Literal["subagent_started"] = "subagent_started"
    call_id: str
    name: str


class SubagentFinished(Event):
    type: Literal["subagent_finished"] = "subagent_finished"
    call_id: str
    name: str
    ok: bool


class SqlExecuted(Event):
    type: Literal["sql_executed"] = "sql_executed"
    sql: str
    purpose: str
    duration_ms: float | None
    row_count: int | None
    plan_cost: float | None


class SqlRejected(Event):
    type: Literal["sql_rejected"] = "sql_rejected"
    sql: str
    purpose: str
    reason: str


class RunFinished(Event):
    type: Literal["run_finished"] = "run_finished"
    run_id: str
    status: str


LimitKind = Literal["tokens", "tool_calls", "queries"]


class LimitReached(Event):
    type: Literal["limit_reached"] = "limit_reached"
    limit: LimitKind
    used: int
    max: int


class Usage(Event):
    type: Literal["usage"] = "usage"
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float | None


class Error(Event):
    type: Literal["error"] = "error"
    message: str


class Done(Event):
    """Last event of every Turn. `ok` is False when the Turn ended on an Error or was
    cancelled; `cancelled` tells the two apart. `answer` is what was answered before it ended."""

    type: Literal["done"] = "done"
    thread_id: str
    answer: str
    ok: bool = True
    cancelled: bool = False


AgentEvent = Annotated[
    Token
    | ToolStarted
    | ToolFinished
    | SubagentStarted
    | SubagentFinished
    | SqlExecuted
    | SqlRejected
    | RunFinished
    | LimitReached
    | Usage
    | Error
    | Done,
    Field(discriminator="type"),
]
_EVENT: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)
_EVENT_TYPES = {
    m.model_fields["type"].default
    for m in (
        Token,
        ToolStarted,
        ToolFinished,
        SubagentStarted,
        SubagentFinished,
        SqlExecuted,
        SqlRejected,
        RunFinished,
        LimitReached,
        Usage,
        Error,
        Done,
    )
}


def from_audit(entry: AuditEntry) -> SqlExecuted | SqlRejected:
    """The event for one audited statement."""
    if entry.decision == "rejected":
        return SqlRejected(sql=entry.sql, purpose=entry.purpose, reason=entry.reason or "")
    return SqlExecuted(
        sql=entry.sql,
        purpose=entry.purpose,
        duration_ms=entry.duration_ms,
        row_count=entry.row_count,
        plan_cost=entry.plan_cost,
    )


SUMMARY_CHARS = 200


_TRAILING_WORD = re.compile(r"[A-Za-z0-9_$]*$")


class Translator:
    """Stateful: collects the answer text across one turn. With `aliases`, the model's
    identifier aliases are shown as real names; a token's trailing word is held back until the
    next chunk, since an alias may be split across chunks. Call `flush` at the end."""

    def __init__(self, aliases: Aliases | None = None) -> None:
        self.answer = ""
        self._aliases = aliases
        self._pending = ""
        self._subagents: dict[str, str] = {}  # delegating call id -> subagent name

    def translate(self, mode: str, chunk: Any, namespace: tuple[str, ...] = ()) -> list[AgentEvent]:
        """`namespace` is LangGraph's subgraph path: empty for the orchestrator, non-empty for a
        subagent, whose tool and SQL events stream but whose text is a report to the
        orchestrator, not the answer."""
        subagent = bool(namespace)
        events = self._translate(mode, chunk, subagent)
        if self._aliases is None:
            return events
        out: list[AgentEvent] = []
        for e in events:
            if isinstance(e, Token):
                out += self._hold(e.text)
            else:
                out += self.flush()
                if isinstance(e, ToolStarted) and not subagent:
                    self.answer = ""  # the flushed text was a preamble too
                out.append(self._unalias(e, self._aliases))
        return out

    def flush(self) -> list[AgentEvent]:
        text, self._pending = self._pending, ""
        return self._emit(text)

    def _hold(self, text: str) -> list[AgentEvent]:
        self._pending += text
        cut = _TRAILING_WORD.search(self._pending)
        start = cut.start() if cut else len(self._pending)
        ready, self._pending = self._pending[:start], self._pending[start:]
        return self._emit(ready)

    def _emit(self, aliased: str) -> list[AgentEvent]:
        if not aliased or self._aliases is None:
            return []
        text = self._aliases.unalias(aliased)
        self.answer += text
        return [Token(text=text)]

    def _unalias(self, event: AgentEvent, aliases: Aliases) -> AgentEvent:
        if isinstance(event, ToolStarted):
            return event.model_copy(update={"args": aliases.unalias_args(event.args)})
        if isinstance(event, ToolFinished):
            return event.model_copy(update={"summary": aliases.unalias(event.summary)})
        return event

    def _translate(self, mode: str, chunk: Any, subagent: bool = False) -> list[AgentEvent]:
        if mode == "messages":
            msg, meta = chunk
            if subagent or meta.get("langgraph_node") != "model":
                return []
            return self._tokens(msg)
        if mode == "updates":
            return [e for update in chunk.values() for e in self._update(update, subagent)]
        if mode == "custom":
            # Our tools write AgentEvents; libraries may write other payloads, which we skip.
            if isinstance(chunk, dict) and chunk.get("type") in _EVENT_TYPES:
                return [_EVENT.validate_python(chunk)]
            return []
        return []

    def _tokens(self, msg: BaseMessage) -> list[AgentEvent]:
        if not isinstance(msg, AIMessage | AIMessageChunk) or msg.name == TURN_LIMIT:
            return []
        if text := msg.text:
            if self._aliases is None:
                self.answer += text
            return [Token(text=text)]
        return []

    def _update(self, update: Any, subagent: bool = False) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        for msg in (update or {}).get("messages", []) if isinstance(update, dict) else []:
            if isinstance(msg, AIMessage) and msg.name == TURN_LIMIT:
                meta = msg.response_metadata
                events.append(LimitReached(limit=meta["limit"], used=meta["used"], max=meta["max"]))
            elif isinstance(msg, AIMessage):
                if msg.tool_calls and not subagent:
                    self.answer = ""  # text before a tool call is a preamble, not the answer
                for c in msg.tool_calls:
                    call_id = c["id"] or ""
                    events.append(ToolStarted(call_id=call_id, name=c["name"], args=c["args"]))
                    if c["name"] == DELEGATE and not subagent:
                        name = self._subagents[call_id] = str(c["args"].get(SUBAGENT_ARG, ""))
                        events.append(SubagentStarted(call_id=call_id, name=name))
            elif isinstance(msg, ToolMessage):
                ok = msg.status != "error"
                if not subagent and (name := self._subagents.pop(msg.tool_call_id, "")):
                    events.append(SubagentFinished(call_id=msg.tool_call_id, name=name, ok=ok))
                events.append(
                    ToolFinished(
                        call_id=msg.tool_call_id,
                        name=msg.name or "",
                        ok=ok,
                        summary=str(msg.content)[:SUMMARY_CHARS],
                    )
                )
        return events
