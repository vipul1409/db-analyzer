"""The HTTP API against the seeded database: chat over SSE (replayed from the agent tests'
cassettes), deterministic Runs without a model key, Findings, comparison, export and audit."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from db_analyzer.agent.llm import LLMSettings
from db_analyzer.service import AnalyzerService
from tests.api import client, eventually, post_then_disconnect, run, sse
from tests.fixtures.dataset import GROUND_TRUTH

from .conftest import SUPPORTED, DsnEnv, Shop, seeded_dsn
from .test_agent_chat import CASSETTES, RECORDED_ON, RECORDING

pytestmark = pytest.mark.integration

# Replayed, never re-recorded here: re-recording test_agent_chat.py updates these too.
ANALYSE_STORAGE = (
    CASSETTES / "test_analyse_storage_delegates_to_the_inventory_analyst_and_ranks_findings"
)
replayed = pytest.mark.skipif(
    RECORDING or RECORDED_ON not in SUPPORTED, reason=f"replays PG {RECORDED_ON} cassettes"
)


@pytest.fixture
def no_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture
def chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_model_key: None) -> AnalyzerService:
    """A service replaying the "Analyse storage" Turn, on a Connection named shop."""
    monkeypatch.setenv("DBX_TEST_SHOP_DSN", seeded_dsn(RECORDED_ON))
    settings = LLMSettings(model="gpt-5.4-mini", cassette=ANALYSE_STORAGE)
    service = AnalyzerService(home=tmp_path / "chat", llm=settings)
    service.add_connection("shop", dsn_env="DBX_TEST_SHOP_DSN")
    return service


async def shop_thread(c: httpx.AsyncClient) -> tuple[str, str]:
    [connection] = (await c.get("/api/connections")).json()
    thread = (await c.post(f"/api/connections/{connection['id']}/threads")).json()
    return connection["id"], thread["id"]


@replayed
def test_chat_streams_a_subagent_turn_and_what_it_did_is_kept(chat: AnalyzerService) -> None:
    async def go() -> dict[str, Any]:
        async with client(chat) as c:
            connection, thread = await shop_thread(c)
            r = await c.post(f"/api/threads/{thread}/messages", json={"message": "Analyse storage"})
            assert r.status_code == 200
            return {
                "events": sse(r.text),
                "audit": (await c.get(f"/api/connections/{connection}/audit?thread_id={thread}")),
                "threads": (await c.get(f"/api/connections/{connection}/threads")).json(),
                "history": (await c.get(f"/api/threads/{thread}/history")).json(),
                "usage": (await c.get(f"/api/threads/{thread}/usage")).json(),
                "month": (await c.get("/api/usage")).json(),
                "runs": (await c.get(f"/api/connections/{connection}/runs")).json(),
                "thread": thread,
            }

    got = run(go())

    events = got["events"]
    types = [e["type"] for e in events]
    assert "error" not in types
    delegated = types.index("subagent_started")
    started = events[delegated]
    assert started["name"] == "inventory-analyst"
    assert events[delegated - 1] == {
        **events[delegated - 1],
        "type": "tool_started",
        "name": "task",
        "call_id": started["call_id"],
    }
    finished = types.index("subagent_finished")
    assert events[finished] == {**started, "type": "subagent_finished", "ok": True}
    inside = types[delegated:finished]
    assert "sql_executed" in inside and "run_finished" in inside, "the subagent's work"
    assert types[-2:] == ["usage", "done"]
    done = events[-1]
    assert done["ok"] and not done["cancelled"]

    streamed = [e["sql"] for e in events if e["type"] == "sql_executed"]
    audited = [a["sql"] for a in got["audit"].json() if a["decision"] == "executed"]
    assert audited == streamed, "the audit lists exactly the statements streamed"

    [listed] = got["threads"]
    assert (listed["id"], listed["preview"]) == (got["thread"], "Analyse storage")
    assert got["history"] == [
        {"role": "user", "text": "Analyse storage"},
        {"role": "agent", "text": done["answer"]},
    ]
    assert got["usage"] == events[-2]
    assert got["month"] == events[-2], "the only Thread this month"
    [chat_run] = got["runs"]
    [finished_run] = [e for e in events if e["type"] == "run_finished"]
    assert (chat_run["id"], chat_run["thread_id"]) == (finished_run["run_id"], got["thread"])


@replayed
def test_cancelling_mid_turn_ends_it_with_what_it_had_done(chat: AnalyzerService) -> None:
    async def go() -> tuple[httpx.Response, httpx.Response, list[Any]]:
        async with client(chat) as c:
            connection, thread = await shop_thread(c)
            turn = asyncio.create_task(
                c.post(f"/api/threads/{thread}/messages", json={"message": "Analyse storage"})
            )

            async def querying() -> bool:
                audit = await c.get(f"/api/connections/{connection}/audit?thread_id={thread}")
                return bool(audit.json())

            await eventually(querying, timeout=60)
            cancel = await c.post(f"/api/threads/{thread}/cancel")
            done = await turn
            audit = await c.get(f"/api/connections/{connection}/audit?thread_id={thread}")
            return cancel, done, audit.json()

    cancel, done, audit = run(go())

    assert cancel.json() == {"cancelled": True}
    events = sse(done.text)
    assert [e["type"] for e in events][-2:] == ["usage", "done"]
    assert (events[-1]["ok"], events[-1]["cancelled"]) == (False, True)
    streamed = [e for e in events if e["type"] == "sql_executed"]
    assert len(streamed) <= len(audit), "statements already running still finish and are audited"


@replayed
def test_closing_the_stream_cancels_the_turn(chat: AnalyzerService) -> None:
    async def go() -> tuple[list[Any], httpx.Response]:
        async with client(chat) as c:
            connection, thread = await shop_thread(c)

            async def querying() -> None:
                async def audited() -> bool:
                    r = await c.get(f"/api/connections/{connection}/audit?thread_id={thread}")
                    return bool(r.json())

                await eventually(audited, timeout=60)

            path = f"/api/threads/{thread}/messages"
            sent = await post_then_disconnect(chat, path, {"message": "Analyse storage"}, querying)
            return sent, await c.post(f"/api/threads/{thread}/cancel")

    sent, cancel = run(go())

    assert (sent[-1]["type"], sent[-1]["cancelled"]) == ("done", True)
    assert cancel.json() == {"cancelled": False}, "nothing left running"


# --- Connections ----------------------------------------------------------------------------


def test_probe_is_kept_and_shown_again_without_touching_the_database(
    service: AnalyzerService, shop: Shop
) -> None:
    async def go() -> tuple[httpx.Response, httpx.Response, list[Any]]:
        async with client(service) as c:
            probed = await c.post(f"/api/connections/{shop.id}/probe")
            audited = (await c.get(f"/api/connections/{shop.id}/audit")).json()
            latest = await c.get(f"/api/connections/{shop.id}/probe")
            after = (await c.get(f"/api/connections/{shop.id}/audit")).json()
            assert after == audited, "reading the latest probe sends no SQL"
            return probed, latest, audited

    probed, latest, audited = run(go())

    assert probed.status_code == 200 and audited
    assert latest.json() == probed.json()
    assert probed.json()["server_version_num"] // 10000 == shop.major
    assert probed.json()["in_recovery"] is False, "the fixture is a primary"


def test_a_writable_login_is_refused_with_its_reason(
    service: AnalyzerService, dsn_env: DsnEnv
) -> None:
    async def go() -> httpx.Response:
        async with client(service) as c:
            body = {"name": "writer", "dsn_env": dsn_env(SUPPORTED[0], "db_writer")}
            connection = (await c.post("/api/connections", json=body)).json()
            return await c.post(f"/api/connections/{connection['id']}/probe")

    r = run(go())

    assert (r.status_code, r.json()["code"]) == (422, "connection_refused")
    assert "write" in r.json()["message"]


# --- Runs, Findings and reports, with no model key -------------------------------------------


def test_runs_need_no_model_key(service: AnalyzerService, shop: Shop, no_model_key: None) -> None:
    async def go() -> dict[str, Any]:
        async with client(service) as c:
            health = (await c.get("/api/health")).json()
            runs = f"/api/connections/{shop.id}/runs"
            inventory = await c.post(runs, json={})
            targeted = await c.post(
                runs, json={"collections": ["public.events"], "exact_counts": True}
            )
            workload = await c.post(
                runs, json={"analyzers": ["workload"], "min_stats_window_hours": 0}
            )
            unknown = await c.post(runs, json={"collections": ["public.nope"]})
            return {
                "health": health,
                "inventory": inventory.json(),
                "targeted": (await c.get(f"/api/runs/{targeted.json()['id']}")).json(),
                "workload": (await c.get(f"/api/runs/{workload.json()['id']}")).json(),
                "unknown": unknown,
                "listed": (await c.get(runs)).json(),
            }

    got = run(go())

    assert got["health"]["chat_available"] is False
    assert got["inventory"]["status"] in ("complete", "partial")
    assert got["inventory"]["thread_id"] is None, "not from chat"
    largest = GROUND_TRUTH["inventory"]["largest_table"]
    measured = {f"{r['namespace']}.{r['name']}" for r in got["inventory"]["scope"]["inventory"]}
    assert largest in measured
    [events] = got["targeted"]["storage"]
    assert (events["ref"]["name"], events["row_count_method"]) == ("events", "exact")
    ranked = got["workload"]["workload"]["items"]
    assert any("FROM bookings WHERE account_id = $1" in i["text"] for i in ranked)
    assert (got["unknown"].status_code, got["unknown"].json()["code"]) == (
        422,
        "unknown_collections",
    )
    assert got["unknown"].json()["details"] == {"collections": ["public.nope"]}
    statuses = [r["status"] for r in got["listed"]]
    assert len(statuses) == 4 and statuses[-1] == "failed", "the refused Run is not a clean one"


def test_findings_move_through_their_lifecycle_and_runs_compare_and_export(
    service: AnalyzerService, shop: Shop, no_model_key: None
) -> None:
    unused = "unused_index:public.idx_accounts_created_at"

    async def go() -> dict[str, Any]:
        async with client(service) as c:
            base = f"/api/connections/{shop.id}"
            first = (await c.post(f"{base}/runs", json={})).json()
            board = (await c.get(f"{base}/findings")).json()
            acked = await c.post(
                f"{base}/findings/status", json={"fingerprint": unused, "status": "acknowledged"}
            )
            obsolete_too = await c.get(f"{base}/findings?status=open&status=obsolete")
            second = (await c.post(f"{base}/runs", json={})).json()
            return {
                "board": board,
                "acked": acked.json(),
                "after": (await c.get(f"{base}/findings?status=acknowledged")).json(),
                "obsolete_too": obsolete_too,
                "observations": (
                    await c.get(f"{base}/observations", params={"fingerprint": unused})
                ).json(),
                "compare": (
                    await c.get(
                        "/api/runs/compare", params={"before": first["id"], "after": second["id"]}
                    )
                ).json(),
                "md": await c.get(f"/api/runs/{second['id']}/export?format=md"),
                "json": await c.get(f"/api/runs/{second['id']}/export?format=json"),
                "bad_status": await c.post(
                    f"{base}/findings/status", json={"fingerprint": unused, "status": "obsolete"}
                ),
                "second": second,
            }

    got = run(go())

    by_fingerprint = {v["finding"]["fingerprint"]: v for v in got["board"]}
    seeded = {f["fingerprint"] for f in GROUND_TRUTH["findings"]}
    by_inventory = {f for f in seeded if f.split(":")[0] not in ("hotspot", "missing_index")}
    assert by_inventory <= set(by_fingerprint)
    view = by_fingerprint[unused]
    assert view["latest"]["severity"] in ("low", "medium", "high")
    assert view["latest"]["title"] and view["finding"]["status"] == "open"
    assert got["acked"]["status"] == "acknowledged"
    assert [v["finding"]["fingerprint"] for v in got["after"]] == [unused]
    assert got["obsolete_too"].status_code == 200
    assert [o["run_id"] for o in got["observations"]][-1] == got["second"]["id"]
    assert len(got["observations"]) == 2, "one per Run that saw it"
    assert got["compare"]["after"] == got["second"]["id"]
    assert got["compare"]["shared"]["inventory"], "both Runs measured every table"
    assert got["compare"]["appeared"] == [] and got["compare"]["disappeared"] == []
    md, js = got["md"], got["json"]
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.headers["content-disposition"] == (
        f'attachment; filename="run-{got["second"]["id"][:8]}.md"'
    )
    assert md.text == service.export(got["second"]["id"], "md").decode()
    assert js.headers["content-type"] == "application/json"
    assert js.json()["run"]["id"] == got["second"]["id"]
    assert (got["bad_status"].status_code, got["bad_status"].json()["code"]) == (
        422,
        "invalid_request",
    ), "obsolete is set by Runs only"
