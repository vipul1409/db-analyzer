"""The workload tools' backend in a Turn, without a model: what the workload-analyst sees."""

from pathlib import Path

import pytest

from db_analyzer.service import AgentTurn, AnalyzerService

from .conftest import Shop
from .test_workload import ANY_WINDOW, SLOW

pytestmark = pytest.mark.integration


@pytest.fixture
def turn(shop: Shop, tmp_path: Path) -> AgentTurn:
    # The seed reset the statistics minutes ago: let Turns rank them anyway.
    service = AnalyzerService(home=tmp_path, min_stats_window=ANY_WINDOW)
    return AgentTurn(service, service.start_thread(shop.id))


def test_top_queries_make_a_workload_run_and_explain_each_statement(
    service: AnalyzerService, shop: Shop, turn: AgentTurn
) -> None:
    result = turn.top_queries(25)

    [run] = service.runs(shop.id)
    assert result["run_id"] == run.id and run.status == "complete"
    queries = result["queries"]
    assert [q["rank"] for q in queries] == list(range(1, len(queries) + 1))
    for expected in SLOW:
        [q] = [q for q in queries if expected["match"] in q["statement"]]
        assert set(q["plan_rules"]) == set(expected["plan_rules"]), q
        assert q["explanation"], "the plan rules say why"


def test_top_n_limits_the_statements_listed(turn: AgentTurn) -> None:
    result = turn.top_queries(2)

    assert len(result["queries"]) == 2
    assert result["ranked"] > 2


def test_query_details_reuse_the_latest_run_and_send_no_sql(
    service: AnalyzerService, shop: Shop, turn: AgentTurn
) -> None:
    top = turn.top_queries(10)
    ranking = top["queries"]
    runs, audited = service.runs(shop.id), service.audit(shop.id)

    details = turn.query_details(3)

    assert service.runs(shop.id) == runs, "a follow-up is not a Run"
    assert service.audit(shop.id) == audited, "and sends nothing to the database"
    assert details["rank"] == 3 and details["finding"] == ranking[2]["finding"]
    assert details["run_id"] == top["run_id"]
    assert details["text"].startswith(ranking[2]["statement"].rstrip("…"))
    assert details["plan"] or details["plan_skipped"]


def test_query_details_name_the_rule_and_the_plan_node_it_quotes(turn: AgentTurn) -> None:
    ranking = turn.top_queries(25)["queries"]
    [rank] = [q["rank"] for q in ranking if "FROM events ORDER BY k" in q["statement"]]

    details = turn.query_details(rank)

    [rule] = details["plan_rules"]
    assert rule["rule"] == "spill_prone_sort_or_hash"
    assert rule["node"] in [line.strip() for line in details["plan"]]


def test_query_details_outside_the_ranking_say_so(turn: AgentTurn) -> None:
    ranked = turn.top_queries(5)["ranked"]

    assert "error" in turn.query_details(ranked + 1)


def test_query_details_before_any_workload_run_say_to_rank_first(turn: AgentTurn) -> None:
    result = turn.query_details(1)

    assert "get_top_queries" in result["error"]
