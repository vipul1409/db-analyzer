"""The agent end to end against the seeded database, with model responses from cassettes.

CI replays tests/cassettes/<test>/turn-N.json with no API key and no network; tools and SQL
run for real. Re-record after changing prompts, tools or the model:

    DBX_LLM_CASSETTE_MODE=record DBX_TEST_PG_VERSIONS=17 \\
        uv run --env-file .env pytest tests/integration/test_agent_chat.py
"""

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from db_analyzer.agent.events import (
    AgentEvent,
    Done,
    Error,
    LimitReached,
    RunFinished,
    SqlExecuted,
    SqlRejected,
    Token,
    ToolFinished,
    ToolStarted,
)
from db_analyzer.agent.llm import LLMSettings
from db_analyzer.core.model import Connection
from db_analyzer.core.units import format_bytes
from db_analyzer.service import AnalyzerService
from tests.fixtures.dataset import GROUND_TRUTH

from .conftest import SUPPORTED, seeded_dsn

CASSETTES = Path(__file__).parents[1] / "cassettes"
RECORDED_ON = 17  # cassettes were recorded against this fixture; sizes in answers depend on it
RECORDING = os.environ.get("DBX_LLM_CASSETTE_MODE") == "record"
BIGGEST = "What are the biggest tables?"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(RECORDED_ON not in SUPPORTED, reason=f"cassettes need PG {RECORDED_ON}"),
]


@dataclass
class Agent:
    home: Path
    settings: LLMSettings
    connection: Connection

    def service(self) -> AnalyzerService:
        """A fresh service on the same home: what a new process would see."""
        return AnalyzerService(home=self.home, llm=self.settings)


@pytest.fixture
def agent(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Agent:
    monkeypatch.setenv("DBX_TEST_SHOP_DSN", seeded_dsn(RECORDED_ON))
    cassette = CASSETTES / request.node.name
    if RECORDING:
        shutil.rmtree(cassette, ignore_errors=True)
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # replay must not need the network
    limits = getattr(request, "param", {})
    settings = LLMSettings(model="gpt-5.4-mini", cassette=cassette, record=RECORDING, **limits)
    service = AnalyzerService(home=tmp_path, llm=settings)
    connection = service.add_connection("shop", dsn_env="DBX_TEST_SHOP_DSN")
    return Agent(tmp_path, settings, connection)


def turn(service: AnalyzerService, thread_id: str, message: str) -> list[AgentEvent]:
    async def collect() -> list[AgentEvent]:
        return [e async for e in service.send(thread_id, message)]

    return asyncio.run(collect())


def of(events: list[AgentEvent], kind: type[Any]) -> list[Any]:
    return [e for e in events if isinstance(e, kind)]


def test_biggest_tables_answer_matches_the_ground_truth(agent: Agent) -> None:
    service = agent.service()
    thread = service.start_thread(agent.connection.id)

    events = turn(service, thread.id, BIGGEST)

    assert not of(events, Error)
    assert "get_storage_stats" in [t.name for t in of(events, ToolStarted)]
    assert of(events, SqlExecuted), "the SQL the tool ran is streamed"
    assert of(events, Token), "the answer is streamed"
    [run] = of(events, RunFinished)
    sizes = {s.ref.qualified: s for s in service.storage(run.run_id)}
    largest = GROUND_TRUTH["inventory"]["largest_table"]
    [done] = of(events, Done)
    assert largest.split(".")[-1] in done.answer
    assert format_bytes(sizes[largest].total_bytes) in done.answer
    assert events[-1] == done and done.ok


def test_analyse_storage_delegates_to_the_inventory_analyst_and_ranks_findings(
    agent: Agent,
) -> None:
    service = agent.service()
    thread = service.start_thread(agent.connection.id)

    events = turn(service, thread.id, "Analyse storage")

    assert not of(events, Error)
    started = of(events, ToolStarted)
    [task] = [t for t in started if t.name == "task"]
    assert task.args["subagent_type"] == "inventory-analyst"
    assert "get_storage_stats" in [t.name for t in started], "the subagent's tools stream"
    assert of(events, SqlExecuted), "and so does its SQL"
    [run] = of(events, RunFinished)
    ranked = [o.fingerprint for o in service.run_observations(run.run_id) if o.severity != "info"]
    assert ranked == [
        "bloat:public.audit_log",
        "stale_stats:public.legacy_imports",
        "size:reference.sku_categories:index_heavy",
    ]
    answer = of(events, Done)[0].answer
    positions = [answer.find(fp.split(":")[1].split(".")[1]) for fp in ranked]
    assert -1 not in positions, answer
    assert positions == sorted(positions), "findings are presented in rank order"


def test_aggregate_ad_hoc_sql_answers_in_chat(agent: Agent) -> None:
    service = agent.service()
    thread = service.start_thread(agent.connection.id)

    events = turn(service, thread.id, "How many bookings have status 'pending'?")

    assert not of(events, Error)
    assert "run_readonly_sql" in [t.name for t in of(events, ToolStarted)]
    assert not of(events, SqlRejected)
    assert [e for e in of(events, SqlExecuted) if e.row_count == 1], "the aggregate ran"
    answer = of(events, Done)[0].answer
    assert "2,000" in answer or "2000" in answer


def test_row_data_the_agent_asks_for_is_rejected_and_audited(agent: Agent) -> None:
    service = agent.service()
    thread = service.start_thread(agent.connection.id)

    events = turn(
        service,
        thread.id,
        "Run exactly this SQL with run_readonly_sql: SELECT email FROM accounts LIMIT 3",
    )

    [rejected, *_] = of(events, SqlRejected)
    assert "email" in rejected.reason
    audit = service.audit(agent.connection.id, thread.id)
    assert ("SELECT email FROM accounts LIMIT 3", "rejected") in [
        (a.sql, a.decision) for a in audit
    ]
    assert "@example.test" not in of(events, Done)[0].answer


def test_thread_survives_a_restart_and_stays_on_its_connection(agent: Agent) -> None:
    first = agent.service()
    thread = first.start_thread(agent.connection.id)
    turn(first, thread.id, BIGGEST)

    restarted = agent.service()
    events = turn(restarted, thread.id, "Which table did you rank first? Reply with its name only.")

    assert restarted.thread(thread.id).connection_id == agent.connection.id
    assert not of(events, ToolStarted), "answered from the restored conversation"
    assert "events" in of(events, Done)[0].answer


def test_turn_activity_is_logged_and_audited(agent: Agent) -> None:
    service = agent.service()
    thread = service.start_thread(agent.connection.id)

    turn(service, thread.id, BIGGEST)

    requests = service.llm_requests(thread.id)
    assert requests and BIGGEST in requests[0].request
    assert service.audit(agent.connection.id, thread.id), "SQL is audited against the Thread"
    usage = service.usage(thread.id)
    assert usage.input_tokens == sum(r.input_tokens for r in requests) > 0
    assert usage.cost_usd is not None and usage.cost_usd > 0


@pytest.mark.parametrize("agent", [{"max_tokens_per_turn": 1}], indirect=True)
def test_token_limit_ends_the_turn_with_a_clean_event(agent: Agent) -> None:
    service = agent.service()
    thread = service.start_thread(agent.connection.id)

    events = turn(service, thread.id, BIGGEST)

    assert not of(events, Error)
    [limit] = of(events, LimitReached)
    assert (limit.limit, limit.max) == ("tokens", 1)
    assert not of(events, SqlExecuted), "the reply's tool calls never ran"
    assert isinstance(events[-1], Done)


@pytest.mark.parametrize("agent", [{"max_tool_calls_per_turn": 0}], indirect=True)
def test_tool_call_limit_stops_the_tool_and_ends_the_turn(agent: Agent) -> None:
    service = agent.service()
    thread = service.start_thread(agent.connection.id)

    events = turn(service, thread.id, BIGGEST)

    assert of(events, ToolFinished) and not any(t.ok for t in of(events, ToolFinished))
    assert not of(events, SqlExecuted), "the refused tool never ran"
    [limit] = of(events, LimitReached)
    assert limit.limit == "tool_calls"
    assert isinstance(events[-1], Done)
