"""Driving the HTTP API in process: httpx's ASGI transport on a real AnalyzerService."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from db_analyzer.api import create_app
from db_analyzer.service import AnalyzerService


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@asynccontextmanager
async def client(
    service: AnalyzerService, token: str | None = None, auth: str | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """A client of an app on `service`. `token` is what the server requires; `auth` what the
    client sends (by default the same)."""
    app = create_app(service, token=token)
    sent = auth if auth is not None else token
    headers = {"Authorization": f"Bearer {sent}"} if sent else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers, timeout=60
    ) as c:
        yield c


def sse(body: str) -> list[dict[str, Any]]:
    """The events of a text/event-stream body, each its data with the SSE event name checked
    against the data's type."""
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        data = json.loads(fields["data"])
        assert fields["event"] == data["type"]
        events.append(data)
    return events


async def post_then_disconnect(
    service: AnalyzerService,
    path: str,
    body: object,
    disconnect_after: Callable[[], Awaitable[None]],
) -> list[dict[str, Any]]:
    """POST `body` at the ASGI level, then disconnect once `disconnect_after` returns, as a
    browser tab closing would. The events the server went on to send, though nobody read them.
    ASGI 2.3, as uvicorn speaks it."""
    app = create_app(service)
    request = {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    received = False
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return request
        await disconnect_after()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
    }
    await asyncio.wait_for(app(scope, receive, send), 60)
    return sse(b"".join(chunks).decode())


async def eventually(check: Callable[[], Awaitable[bool]], timeout: float = 10) -> None:
    """Poll `check` until it holds."""
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.02)


# Tools the orchestrator binds on a Postgres Connection: a hand-written cassette must match.
ORCHESTRATOR_TOOLS = [
    "delete",
    "edit_file",
    "glob",
    "grep",
    "ls",
    "probe",
    "read_file",
    "run_readonly_sql",
    "task",
    "write_file",
]


def answer(
    cassette: Path,
    turn: int,
    text: str,
    model: str = "gpt-5.4-mini",
    tokens: tuple[int, int] = (100, 20),
) -> None:
    """A cassette for Turn `turn` of a Thread whose earlier Turns were answered the same way:
    the model answers `text` straight away, calling no tool."""
    response = {
        "type": "ai",
        "data": {
            "content": text,
            "name": "db_analyzer",
            "response_metadata": {"model_name": model},
            "usage_metadata": {
                "input_tokens": tokens[0],
                "output_tokens": tokens[1],
                "total_tokens": sum(tokens),
            },
        },
    }
    request = {"tools": ORCHESTRATOR_TOOLS, "messages": 2 * turn + 1, "last": "human"}
    cassette.mkdir(parents=True, exist_ok=True)
    (cassette / f"turn-{turn}.json").write_text(
        json.dumps({"calls": [{"request": request, "response": response}]})
    )


@dataclass
class HangingModel:
    """A model endpoint that accepts requests and never answers: a Turn that reaches it stays
    in progress until it is cancelled."""

    url: str
    requests: list[asyncio.StreamWriter] = field(default_factory=list)

    async def reached(self, n: int = 1) -> None:
        """Wait until `n` requests are being held."""

        async def check() -> bool:
            return len(self.requests) >= n

        await eventually(check)


@asynccontextmanager
async def hanging_model() -> AsyncIterator[HangingModel]:
    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        model.requests.append(writer)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    model = HangingModel(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1")
    try:
        yield model
    finally:
        for w in model.requests:
            w.close()
        server.close()
        await server.wait_closed()
