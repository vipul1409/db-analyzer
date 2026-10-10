"""The HTTP API without a database: auth, errors, Connections, Threads, Turns and Runs as far
as they go before any SQL is sent."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from db_analyzer.agent.llm import LLMSettings
from db_analyzer.service import AnalyzerService
from tests.api import answer, client, hanging_model, post_then_disconnect, run, sse


@pytest.fixture
def request_fixture(request: pytest.FixtureRequest) -> pytest.FixtureRequest:
    return request


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AnalyzerService:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return AnalyzerService(home=tmp_path, llm=LLMSettings())


def get(service: AnalyzerService, path: str, **kw: str | None) -> httpx.Response:
    async def go() -> httpx.Response:
        async with client(service, **kw) as c:
            return await c.get(path)

    return run(go())


# --- Health, auth and errors -------------------------------------------------------------------


def test_health_reports_the_version_and_that_chat_needs_a_key(
    service: AnalyzerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = get(service, "/api/health")

    assert r.status_code == 200
    assert r.json() == {"version": "0.1.0", "chat_available": False}

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert get(service, "/api/health").json()["chat_available"] is True


def test_without_a_token_configured_requests_need_none(service: AnalyzerService) -> None:
    assert get(service, "/api/connections").status_code == 200


def test_with_a_token_configured_every_route_but_health_needs_it(
    service: AnalyzerService,
) -> None:
    missing = get(service, "/api/connections", token="s3cret", auth="")
    wrong = get(service, "/api/connections", token="s3cret", auth="guess")
    right = get(service, "/api/connections", token="s3cret")
    health = get(service, "/api/health", token="s3cret", auth="")

    assert (missing.status_code, wrong.status_code, right.status_code) == (401, 401, 200)
    assert missing.json()["code"] == wrong.json()["code"] == "unauthorized"
    assert health.status_code == 200


def test_errors_share_one_shape(service: AnalyzerService) -> None:
    unknown_route = get(service, "/api/nowhere")
    unknown_connection = get(service, "/api/connections/nope")

    assert unknown_route.status_code == unknown_connection.status_code == 404
    for r in (unknown_route, unknown_connection):
        assert set(r.json()) == {"code", "message", "details"}
    assert unknown_connection.json()["code"] == "not_found"


# --- Connections -------------------------------------------------------------------------------


def request(
    service: AnalyzerService, method: str, path: str, body: object = None
) -> httpx.Response:
    async def go() -> httpx.Response:
        async with client(service) as c:
            return await c.request(method, path, json=body)

    return run(go())


def test_a_connection_is_added_by_env_var_name_and_says_whether_it_is_set(
    service: AnalyzerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOP_DSN", "postgresql://u:secret@db/shop")

    added = request(service, "POST", "/api/connections", {"name": "shop", "dsn_env": "SHOP_DSN"})
    other = request(service, "POST", "/api/connections", {"name": "a", "dsn_env": "UNSET_DSN"})

    assert added.status_code == 200
    body = added.json()
    assert (body["name"], body["dsn_env"], body["dsn_env_set"]) == ("shop", "SHOP_DSN", True)
    assert "secret" not in added.text
    listed = request(service, "GET", "/api/connections").json()
    assert [(c["name"], c["dsn_env_set"]) for c in listed] == [("a", False), ("shop", True)]
    assert request(service, "GET", f"/api/connections/{other.json()['id']}").json()["name"] == "a"


def test_updating_a_connection_by_name_keeps_settings_left_out(service: AnalyzerService) -> None:
    first = request(
        service,
        "POST",
        "/api/connections",
        {
            "name": "shop",
            "dsn_env": "SHOP_DSN",
            "limits": {"statement_timeout": "5s", "lock_timeout": "1s", "work_mem": "8MB"},
            "gate": {"max_total_cost": 1000, "max_result_rows": 10, "max_scan_rows": 100},
            "alias_identifiers": True,
        },
    ).json()

    again = request(
        service, "POST", "/api/connections", {"name": "shop", "dsn_env": "OTHER_DSN"}
    ).json()

    assert again["id"] == first["id"]
    assert again["dsn_env"] == "OTHER_DSN"
    assert again["limits"]["statement_timeout"] == "5s"
    assert again["gate"]["max_total_cost"] == 1000
    assert again["alias_identifiers"] is True


def test_probing_without_the_dsn_variable_says_which_one_to_set(
    service: AnalyzerService,
) -> None:
    c = request(service, "POST", "/api/connections", {"name": "s", "dsn_env": "UNSET_DSN"}).json()

    r = request(service, "POST", f"/api/connections/{c['id']}/probe")

    assert r.status_code == 422
    assert r.json()["code"] == "dsn_env_missing"
    assert r.json()["details"] == {"dsn_env": "UNSET_DSN"}


def test_a_connection_never_probed_has_no_latest_probe(service: AnalyzerService) -> None:
    c = request(service, "POST", "/api/connections", {"name": "s", "dsn_env": "X"}).json()

    r = request(service, "GET", f"/api/connections/{c['id']}/probe")

    assert (r.status_code, r.json()) == (200, None)
    assert request(service, "GET", "/api/connections/nope/probe").status_code == 404


def test_capabilities_are_listed(service: AnalyzerService) -> None:
    c = request(service, "POST", "/api/connections", {"name": "s", "dsn_env": "X"}).json()

    r = request(service, "GET", f"/api/connections/{c['id']}/capabilities")

    assert "storage_stats" in r.json()


# --- Threads and Turns -------------------------------------------------------------------------

QUESTION = "What are the biggest tables?"


@pytest.fixture
def cassette(tmp_path: Path) -> Path:
    return tmp_path / "cassette"


@pytest.fixture
def replaying(tmp_path: Path, cassette: Path, monkeypatch: pytest.MonkeyPatch) -> AnalyzerService:
    """Model answers come from `cassette`: Turns that call no tool need no database."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return AnalyzerService(home=tmp_path / "home", llm=LLMSettings(cassette=cassette))


async def new_thread(c: httpx.AsyncClient, name: str = "shop") -> str:
    connection = (await c.post("/api/connections", json={"name": name, "dsn_env": "X"})).json()
    thread = await c.post(f"/api/connections/{connection['id']}/threads")
    assert thread.status_code == 200
    assert thread.json()["connection_id"] == connection["id"]
    return str(thread.json()["id"])


def test_a_message_streams_its_turn_as_server_sent_events(
    replaying: AnalyzerService, cassette: Path
) -> None:
    answer(cassette, 0, "The largest is public.events.")

    async def go() -> httpx.Response:
        async with client(replaying) as c:
            thread = await new_thread(c)
            return await c.post(f"/api/threads/{thread}/messages", json={"message": QUESTION})

    r = run(go())

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse(r.text)
    assert [e["type"] for e in events][-2:] == ["usage", "done"]
    tokens = "".join(e["text"] for e in events if e["type"] == "token")
    assert tokens == "The largest is public.events."
    done = events[-1]
    assert (done["ok"], done["cancelled"], done["answer"]) == (True, False, tokens)
    assert events[-2]["input_tokens"] == 100


def test_a_thread_keeps_its_history_and_is_listed_by_latest_activity(
    replaying: AnalyzerService, cassette: Path
) -> None:
    answer(cassette, 0, "First answer.")
    answer(cassette, 1, "Second answer.")

    async def go() -> tuple[str, str, list[Any], list[Any]]:
        async with client(replaying) as c:
            older = await new_thread(c)
            connection = (await c.get(f"/api/threads/{older}")).json()["connection_id"]
            newer = (await c.post(f"/api/connections/{connection}/threads")).json()["id"]
            await c.post(f"/api/threads/{older}/messages", json={"message": QUESTION})
            await c.post(f"/api/threads/{older}/messages", json={"message": "And the second?"})
            history = (await c.get(f"/api/threads/{older}/history")).json()
            listed = (await c.get(f"/api/connections/{connection}/threads")).json()
            return older, newer, history, listed

    older, newer, history, listed = run(go())

    assert history == [
        {"role": "user", "text": QUESTION},
        {"role": "agent", "text": "First answer."},
        {"role": "user", "text": "And the second?"},
        {"role": "agent", "text": "Second answer."},
    ]
    assert [t["id"] for t in listed] == [older, newer], "the active Thread first"
    assert listed[0]["preview"] == QUESTION, "the first message, not the latest"
    assert listed[0]["last_active_at"] is not None
    assert (listed[1]["preview"], listed[1]["last_active_at"]) == (None, None)


def test_chat_without_a_model_key_is_refused_up_front(service: AnalyzerService) -> None:
    async def go() -> httpx.Response:
        async with client(service) as c:
            thread = await new_thread(c)
            return await c.post(f"/api/threads/{thread}/messages", json={"message": QUESTION})

    r = run(go())

    assert (r.status_code, r.json()["code"]) == (503, "chat_unavailable")


@pytest.mark.parametrize("chat", ["replaying", "service"])
def test_a_message_to_an_unknown_thread_is_not_found(
    chat: str, request_fixture: pytest.FixtureRequest
) -> None:
    """Whether or not chat is available."""
    service: AnalyzerService = request_fixture.getfixturevalue(chat)

    r = request(service, "POST", "/api/threads/nope/messages", {"message": QUESTION})

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert request(service, "POST", "/api/threads/nope/cancel").status_code == 404


@pytest.fixture
def hanging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AnalyzerService:
    """A service whose model is reached over HTTP: point OPENAI_BASE_URL at a hanging model."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return AnalyzerService(home=tmp_path / "home", llm=LLMSettings(max_retries=0))


def test_a_second_message_while_a_turn_runs_is_refused_and_cancel_ends_the_first(
    hanging: AnalyzerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def go() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response]:
        async with hanging_model() as model, client(hanging) as c:
            monkeypatch.setenv("OPENAI_BASE_URL", model.url)
            thread = await new_thread(c)
            first = asyncio.create_task(
                c.post(f"/api/threads/{thread}/messages", json={"message": QUESTION})
            )
            await model.reached()
            second = await c.post(f"/api/threads/{thread}/messages", json={"message": "again"})
            cancel = await c.post(f"/api/threads/{thread}/cancel")
            again = await c.post(f"/api/threads/{thread}/cancel")
            return second, cancel, again, await first

    second, cancel, again, first = run(go())

    assert (second.status_code, second.json()["code"]) == (409, "turn_active")
    assert cancel.json() == {"cancelled": True}
    assert again.json() == {"cancelled": False}, "no Turn left to cancel"
    events = sse(first.text)
    assert [e["type"] for e in events][-2:] == ["usage", "done"]
    assert (events[-1]["ok"], events[-1]["cancelled"]) == (False, True)


def test_a_cancelled_turn_leaves_the_thread_usable(
    hanging: AnalyzerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def go() -> list[httpx.Response]:
        async with hanging_model() as model, client(hanging) as c:
            monkeypatch.setenv("OPENAI_BASE_URL", model.url)
            thread = await new_thread(c)
            done = []
            for n in (1, 2):
                turn = asyncio.create_task(
                    c.post(f"/api/threads/{thread}/messages", json={"message": QUESTION})
                )
                await model.reached(n)
                await c.post(f"/api/threads/{thread}/cancel")
                done.append(await turn)
            return done

    for r in run(go()):
        assert sse(r.text)[-1]["cancelled"] is True


def test_losing_the_stream_cancels_the_turn(
    hanging: AnalyzerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def go() -> tuple[list[dict[str, Any]], httpx.Response]:
        async with hanging_model() as model, client(hanging) as c:
            monkeypatch.setenv("OPENAI_BASE_URL", model.url)
            thread = await new_thread(c)
            path = f"/api/threads/{thread}/messages"
            sent = await post_then_disconnect(hanging, path, {"message": QUESTION}, model.reached)
            return sent, await c.post(f"/api/threads/{thread}/cancel")

    sent, cancel = run(go())

    assert (sent[-1]["type"], sent[-1]["cancelled"]) == ("done", True)
    assert cancel.json() == {"cancelled": False}, "the Turn already ended"


# --- Usage ---------------------------------------------------------------------------------------


def test_usage_adds_up_every_thread_in_the_month_and_unknown_prices_stay_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    priced, unpriced = tmp_path / "priced", tmp_path / "unpriced"
    answer(priced, 0, "Priced.", tokens=(1000, 100))
    answer(unpriced, 0, "Unpriced.", model="mystery-model", tokens=(10, 1))
    home = tmp_path / "home"

    async def ask(cassette: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
        service = AnalyzerService(home=home, llm=LLMSettings(cassette=cassette))
        async with client(service) as c:
            thread = await new_thread(c)
            await c.post(f"/api/threads/{thread}/messages", json={"message": QUESTION})
            month = (await c.get("/api/usage")).json()
            return thread, (await c.get(f"/api/threads/{thread}/usage")).json(), month

    _, first, after_first = run(ask(priced))
    _, second, after_both = run(ask(unpriced))

    assert first["input_tokens"] == after_first["input_tokens"] == 1000
    assert first["cost_usd"] == after_first["cost_usd"] > 0
    assert (second["input_tokens"], second["cost_usd"]) == (10, None)
    assert (after_both["input_tokens"], after_both["output_tokens"]) == (1010, 101)
    assert after_both["cost_usd"] is None, "unknown, not zero"


def test_usage_in_a_month_without_requests_is_zero(service: AnalyzerService) -> None:
    r = get(service, "/api/usage?month=2020-02")

    assert (r.json()["input_tokens"], r.json()["cost_usd"]) == (0, 0)
    assert get(service, "/api/usage?month=2020-13").status_code == 422
    assert get(service, "/api/usage?month=feb").json()["code"] == "invalid_request"


# --- Runs ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code", "details"),
    [
        ({"analyzers": ["hotspot"]}, "unknown_analyzer", {"analyzer": "hotspot"}),
        (
            {"analyzers": ["workload"], "collections": ["public.events"]},
            "options_not_accepted",
            {"options": ["collections"], "analyzer": "inventory"},
        ),
        (
            {"analyzers": ["inventory"], "min_stats_window_hours": 0},
            "options_not_accepted",
            {"options": ["min_stats_window"], "analyzer": "workload"},
        ),
    ],
)
def test_an_invalid_run_request_is_refused_before_anything_runs(
    service: AnalyzerService, body: dict[str, Any], code: str, details: dict[str, Any]
) -> None:
    async def go() -> tuple[httpx.Response, list[Any]]:
        async with client(service) as c:
            connection = (
                await c.post("/api/connections", json={"name": "s", "dsn_env": "X"})
            ).json()["id"]
            r = await c.post(f"/api/connections/{connection}/runs", json=body)
            return r, (await c.get(f"/api/connections/{connection}/runs")).json()

    r, runs = run(go())

    assert (r.status_code, r.json()["code"], r.json()["details"]) == (422, code, details)
    assert runs == [], "no Run was recorded"


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/nope",
        "/api/runs/nope/export?format=md",
        "/api/runs/compare?before=a&after=b",
        "/api/connections/nope/runs",
        "/api/connections/nope/findings",
        "/api/connections/nope/audit",
        "/api/connections/nope/threads",
        "/api/threads/nope",
        "/api/threads/nope/history",
        "/api/threads/nope/usage",
    ],
)
def test_unknown_resources_are_not_found(service: AnalyzerService, path: str) -> None:
    r = get(service, path)

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert r.json()["message"] != "Not Found", "the resource, not the route, is unknown"
