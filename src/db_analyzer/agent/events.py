"""AgentEvent: the typed stream every front end renders (CLI now, API as SSE and UI later).

`Translator` turns LangGraph's stream (`stream_mode=["messages", "updates", "custom"]`) into
events. Tools emit SQL and Run events themselves through LangGraph's stream writer, as plain
dicts of these same models.
"""

from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from pydantic import BaseModel, Field, TypeAdapter

from db_analyzer.core.model import AuditEntry

# Name of the AIMessage the turn-limit middleware adds when it ends a turn.
TURN_LIMIT = "turn_limit"


class Token(BaseModel):
    type: Literal["token"] = "token"
    text: str


class ToolStarted(BaseModel):
    type: Literal["tool_started"] = "tool_started"
    call_id: str
    name: str
    args: dict[str, Any]


class ToolFinished(BaseModel):
    type: Literal["tool_finished"] = "tool_finished"
    call_id: str
    name: str
    ok: bool
    summary: str


class SqlExecuted(BaseModel):
    type: Literal["sql_executed"] = "sql_executed"
    sql: str
    purpose: str
    duration_ms: float | None
    row_count: int | None
    plan_cost: float | None


class SqlRejected(BaseModel):
    type: Literal["sql_rejected"] = "sql_rejected"
    sql: str
    purpose: str
    reason: str


class RunFinished(BaseModel):
    type: Literal["run_finished"] = "run_finished"
    run_id: str
    status: str


LimitKind = Literal["tokens", "tool_calls", "queries"]


class LimitReached(BaseModel):
    type: Literal["limit_reached"] = "limit_reached"
    limit: LimitKind
    used: int
    max: int


class Usage(BaseModel):
    type: Literal["usage"] = "usage"
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float | None


class Error(BaseModel):
    type: Literal["error"] = "error"
    message: str


class Done(BaseModel):
    """Last event of every Turn. `ok` is False when the Turn ended on an Error."""

    type: Literal["done"] = "done"
    thread_id: str
    answer: str
    ok: bool = True


AgentEvent = Annotated[
    Token
    | ToolStarted
    | ToolFinished
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


class Translator:
    """Stateful: collects the answer text across one turn."""

    def __init__(self) -> None:
        self.answer = ""

    def translate(self, mode: str, chunk: Any) -> list[AgentEvent]:
        if mode == "messages":
            msg, meta = chunk
            if meta.get("langgraph_node") != "model":
                return []
            return self._tokens(msg)
        if mode == "updates":
            return [e for update in chunk.values() for e in self._update(update)]
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
            self.answer += text
            return [Token(text=text)]
        return []

    def _update(self, update: Any) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        for msg in (update or {}).get("messages", []) if isinstance(update, dict) else []:
            if isinstance(msg, AIMessage) and msg.name == TURN_LIMIT:
                meta = msg.response_metadata
                events.append(LimitReached(limit=meta["limit"], used=meta["used"], max=meta["max"]))
            elif isinstance(msg, AIMessage):
                if msg.tool_calls:
                    self.answer = ""  # text before a tool call is a preamble, not the answer
                events += [
                    ToolStarted(call_id=c["id"] or "", name=c["name"], args=c["args"])
                    for c in msg.tool_calls
                ]
            elif isinstance(msg, ToolMessage):
                events.append(
                    ToolFinished(
                        call_id=msg.tool_call_id,
                        name=msg.name or "",
                        ok=msg.status != "error",
                        summary=str(msg.content)[:SUMMARY_CHARS],
                    )
                )
        return events
