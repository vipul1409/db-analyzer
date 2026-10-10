"""The HTTP API: a thin adapter over AnalyzerService. Routes only translate HTTP to facade
calls (units, defaults, status codes); they open no database sessions and build no agents.
Sync routes run in FastAPI's thread pool, so a long Run never blocks the event loop."""

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, TypeAdapter
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

import db_analyzer
from db_analyzer import runs
from db_analyzer.agent.events import AgentEvent, Usage
from db_analyzer.analyzers.workload import MIN_STATS_WINDOW
from db_analyzer.api import errors
from db_analyzer.api.errors import ApiError
from db_analyzer.core.comparison import RunComparison
from db_analyzer.core.model import (
    AuditEntry,
    Capability,
    ConnectionInfo,
    Finding,
    FindingStatus,
    FindingView,
    GateLimits,
    Observation,
    ProbeResult,
    Run,
    SessionLimits,
    SettableStatus,
    Thread,
    ThreadMessage,
)
from db_analyzer.core.run_view import RunView
from db_analyzer.service import CURRENT_STATUSES, AnalyzerService

_bearer = HTTPBearer(auto_error=False, description="The token `dbx serve` was given, if any.")


class ConnectionIn(BaseModel):
    """A Connection to add, or to update by name. The DSN is never sent: `dsn_env` names the
    environment variable of the API process that holds it. Settings left out keep their
    current value (the default for a new Connection)."""

    name: str
    dsn_env: str
    limits: SessionLimits | None = None
    gate: GateLimits | None = None
    alias_identifiers: bool | None = None


class RunIn(BaseModel):
    """A deterministic Run: no model. `collections` targets inventory at those tables
    (schema-qualified, or bare when unambiguous) and `exact_counts` counts their rows, each only
    where the EXPLAIN gate allows. `min_stats_window_hours` is how young workload statistics
    may be before ranking is refused (default 1 h). Options no chosen analyzer takes are
    refused."""

    analyzers: list[str] = Field(default_factory=lambda: [str(a) for a in runs.DEFAULT])
    collections: list[str] | None = None
    exact_counts: bool = False
    min_stats_window_hours: float | None = Field(default=None, ge=0)


class StatusIn(BaseModel):
    fingerprint: str
    status: SettableStatus


class MessageIn(BaseModel):
    message: str


class Cancelled(BaseModel):
    cancelled: bool  # whether a Turn was running


class Health(BaseModel):
    version: str
    chat_available: bool  # an OpenAI key is configured; everything else works without one


_EVENTS: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)
_MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"
_EVENT_STREAM: dict[str, Any] = {
    "description": "The Turn's AgentEvents, one per Server-Sent Event.",
    "content": {"text/event-stream": {"schema": {"$ref": "#/components/schemas/AgentEvent"}}},
}


class EventStream(StreamingResponse):
    """A Turn's Server-Sent Events. It streams as ASGI 2.4 asks whatever the server's version:
    Starlette would otherwise listen for the disconnect itself (ASGI < 2.4, as uvicorn speaks)
    and tear the stream down, racing `_sse`, which cancels the Turn through the facade."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.stream_response(send)
        except OSError as e:
            raise ClientDisconnect() from e


async def _sse(
    events: AsyncIterator[AgentEvent], request: Request, cancel: Callable[[], object]
) -> AsyncIterator[str]:
    """`events` as Server-Sent Events. A client that goes away cancels the Turn through the
    facade, as an explicit cancel would; the Turn then ends on its own."""

    async def watch() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass
        cancel()

    watcher = asyncio.create_task(watch())
    try:
        async for e in events:
            yield f"event: {e.type}\ndata: {e.model_dump_json()}\n\n"
    finally:
        watcher.cancel()


def create_app(service: AnalyzerService, token: str | None = None) -> FastAPI:
    """With `token`, every route but health requires it as a bearer token."""

    def authorized(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        if token is None:
            return
        sent = credentials.credentials if credentials else ""
        if not secrets.compare_digest(sent.encode(), token.encode()):
            raise ApiError(401, "unauthorized", "a valid bearer token is required")

    app = FastAPI(title="DB Analyzer", version=db_analyzer.__version__)
    errors.install(app)
    api = APIRouter(
        prefix="/api", dependencies=[Depends(authorized)], responses=errors.ERROR_RESPONSES
    )

    @app.get("/api/health", tags=["health"])
    def health() -> Health:
        return Health(version=db_analyzer.__version__, chat_available=service.chat_available)

    # --- Connections ---------------------------------------------------------------------

    @api.get("/connections", tags=["connections"])
    def list_connections() -> list[ConnectionInfo]:
        return service.connections()

    @api.get("/connections/{connection_id}", tags=["connections"])
    def get_connection(connection_id: str) -> ConnectionInfo:
        return service.connection_info(connection_id)

    @api.post("/connections", tags=["connections"])
    def upsert_connection(body: ConnectionIn) -> ConnectionInfo:
        c = service.add_connection(
            body.name, body.dsn_env, body.limits, body.gate, body.alias_identifiers
        )
        return service.connection_info(c.id)

    @api.post("/connections/{connection_id}/probe", tags=["connections"])
    def probe(connection_id: str) -> ProbeResult:
        """Probe the database now: what the analyzer can see."""
        return service.probe(connection_id)

    @api.get("/connections/{connection_id}/probe", tags=["connections"])
    def latest_probe(connection_id: str) -> ProbeResult | None:
        """The latest probe, without touching the database; null if never probed."""
        return service.latest_probe(connection_id)

    @api.get("/connections/{connection_id}/capabilities", tags=["connections"])
    def capabilities(connection_id: str) -> list[Capability]:
        return sorted(service.capabilities(connection_id))

    # --- Runs ----------------------------------------------------------------------------

    @api.post("/connections/{connection_id}/runs", tags=["runs"])
    def start_run(connection_id: str, body: RunIn) -> Run:
        """Run the analyzers and return the finished Run. Blocks until it finishes."""
        window = body.min_stats_window_hours
        return service.run(
            connection_id,
            body.analyzers,
            collections=body.collections,
            exact_counts=body.exact_counts,
            min_stats_window=MIN_STATS_WINDOW if window is None else timedelta(hours=window),
        )

    @api.get("/connections/{connection_id}/runs", tags=["runs"])
    def list_runs(connection_id: str) -> list[Run]:
        """Oldest first. A Run with a `thread_id` was started from chat."""
        return service.runs(connection_id)

    @api.get("/runs/compare", tags=["runs"])
    def compare_runs(before: str, after: str) -> RunComparison:
        """What changed between two Runs of one Connection, over the scope both measured."""
        return service.compare_runs(before, after)

    @api.get("/runs/{run_id}", tags=["runs"])
    def get_run(run_id: str) -> RunView:
        """Everything the reports show of one Run: ranked observations, sizes, the workload
        ranking and what was skipped."""
        return service.run_view(run_id)

    @api.get(
        "/runs/{run_id}/export",
        tags=["runs"],
        response_class=Response,
        responses={
            200: {
                "description": "The report, as a download.",
                "content": {"text/markdown": {}, "application/json": {}},
            }
        },
    )
    def export_run(run_id: str, format: Literal["md", "json"] = "md") -> Response:
        body = service.export(run_id, format)
        media = "application/json" if format == "json" else "text/markdown; charset=utf-8"
        filename = f"run-{run_id[:8]}.{format}"
        disposition = f'attachment; filename="{filename}"'
        return Response(body, media_type=media, headers={"Content-Disposition": disposition})

    # --- Findings and audit ----------------------------------------------------------------

    @api.get("/connections/{connection_id}/findings", tags=["findings"])
    def list_findings(
        connection_id: str,
        status: Annotated[list[FindingStatus] | None, Query()] = None,
    ) -> list[FindingView]:
        """The Connection's Findings with their latest Observation. Without `status`, every
        Finding but obsolete ones. A Finding with `unobserved_by` set asks "fixed?"."""
        return service.finding_views(connection_id, status or CURRENT_STATUSES)

    @api.post("/connections/{connection_id}/findings/status", tags=["findings"])
    def set_finding_status(connection_id: str, body: StatusIn) -> Finding:
        """Acknowledge, mark fixed or reopen a Finding; settles any "fixed?" prompt."""
        return service.set_finding_status(connection_id, body.fingerprint, body.status)

    @api.get("/connections/{connection_id}/observations", tags=["findings"])
    def observations(connection_id: str, fingerprint: str) -> list[Observation]:
        """One Finding's Observations, one per Run that saw it, oldest first."""
        return service.observations(connection_id, fingerprint)

    @api.get("/connections/{connection_id}/audit", tags=["audit"])
    def audit(connection_id: str, thread_id: str | None = None) -> list[AuditEntry]:
        """Every statement executed or rejected, oldest first; optionally one Thread's."""
        return service.audit(connection_id, thread_id)

    # --- Threads and Turns -----------------------------------------------------------------

    @api.get("/connections/{connection_id}/threads", tags=["threads"])
    def list_threads(connection_id: str) -> list[Thread]:
        """Latest activity first."""
        return service.threads(connection_id)

    @api.post("/connections/{connection_id}/threads", tags=["threads"])
    def start_thread(connection_id: str) -> Thread:
        return service.start_thread(connection_id)

    @api.get("/threads/{thread_id}", tags=["threads"])
    def get_thread(thread_id: str) -> Thread:
        return service.thread(thread_id)

    @api.get("/threads/{thread_id}/history", tags=["threads"])
    async def history(thread_id: str) -> list[ThreadMessage]:
        """The user's messages and the agent's final answers, in order."""
        return await service.history(thread_id)

    @api.post(
        "/threads/{thread_id}/messages",
        tags=["threads"],
        response_class=EventStream,
        responses={200: _EVENT_STREAM},
    )
    async def send(thread_id: str, body: MessageIn, request: Request) -> EventStream:
        """Run one Turn, streaming its AgentEvents as Server-Sent Events: the event name is the
        event's type, the data its JSON. The stream ends after `done`. Closing it cancels the
        Turn. 409 while the Thread has a Turn running; 503 when no model key is configured."""
        service.thread(thread_id)
        if not service.chat_available:
            raise ApiError(
                503, "chat_unavailable", "chat needs OPENAI_API_KEY; Runs work without it"
            )
        events = service.send(thread_id, body.message)
        return EventStream(
            _sse(events, request, lambda: service.cancel(thread_id)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api.post("/threads/{thread_id}/cancel", tags=["threads"])
    def cancel(thread_id: str) -> Cancelled:
        """Cancel the Thread's Turn in progress: its stream ends with `done`, `cancelled`."""
        return Cancelled(cancelled=service.cancel(thread_id))

    @api.get("/threads/{thread_id}/usage", tags=["usage"])
    def thread_usage(thread_id: str) -> Usage:
        return service.usage(thread_id)

    @api.get("/usage", tags=["usage"])
    def monthly_usage(
        month: Annotated[
            str | None,
            Query(pattern=_MONTH, description="YYYY-MM (UTC); default: this month"),
        ] = None,
    ) -> Usage:
        """Every Thread's tokens and estimated cost in one calendar month. `cost_usd` is null
        when any request's cost is unknown."""
        year, number = map(int, month.split("-")) if month else (None, None)
        return service.usage_in_month(date(year, number, 1) if year and number else None)

    app.include_router(api)

    def openapi() -> dict[str, Any]:
        """FastAPI's spec, plus the AgentEvent union the message stream carries."""
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schemas = schema.setdefault("components", {}).setdefault("schemas", {})
            union = _EVENTS.json_schema(
                mode="serialization", ref_template="#/components/schemas/{model}"
            )
            schemas.update(union.pop("$defs"))
            schemas["AgentEvent"] = union
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app
