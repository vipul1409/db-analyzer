import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from db_analyzer.cli import app
from tests.fixtures.dataset import GROUND_TRUTH

from .conftest import SUPPORTED, seeded_dsn

pytestmark = pytest.mark.integration


def test_analyze_writes_markdown_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(SUPPORTED[-1]))
    runner = CliRunner()
    assert runner.invoke(app, ["connect", "shop"]).exit_code == 0
    out = tmp_path / "out.md"

    result = runner.invoke(app, ["analyze", "shop", "--report", str(out)])

    assert result.exit_code == 0, result.output
    assert GROUND_TRUTH["inventory"]["largest_table"] in result.output
    assert out.read_text().startswith("# Inventory report: shop")


def test_analyze_unknown_connection_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path))

    result = CliRunner().invoke(app, ["analyze", "nope"])

    assert result.exit_code == 1
    assert "No Connection named 'nope'" in result.output


def test_analyze_counts_rows_exactly_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(SUPPORTED[-1]))
    runner = CliRunner()
    assert runner.invoke(app, ["connect", "shop"]).exit_code == 0

    result = runner.invoke(
        app, ["analyze", "shop", "--table", "public.tenants", "--exact-counts"], terminal_width=200
    )

    assert result.exit_code == 0, result.output
    assert "public.tenants" in result.output and "public.events" not in result.output
    assert "20 (exact)" in result.output


def test_analyze_shows_ranked_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(SUPPORTED[-1]))
    runner = CliRunner()
    assert runner.invoke(app, ["connect", "shop"]).exit_code == 0

    result = runner.invoke(app, ["analyze", "shop"], terminal_width=200)

    assert result.exit_code == 0, result.output
    assert "1. high bloat:public.audit_log" in result.output


def test_analyze_exports_json_when_the_report_file_ends_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(SUPPORTED[-1]))
    runner = CliRunner()
    assert runner.invoke(app, ["connect", "shop"]).exit_code == 0
    out = tmp_path / "out.json"

    result = runner.invoke(app, ["analyze", "shop", "--report", str(out)])

    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert doc["connection"] == "shop" and doc["run"]["status"] == "complete"


def test_findings_can_be_acknowledged_and_confirmed_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(SUPPORTED[-1]))
    runner = CliRunner()
    assert runner.invoke(app, ["connect", "shop"]).exit_code == 0
    assert runner.invoke(app, ["analyze", "shop"]).exit_code == 0

    acked = runner.invoke(app, ["ack", "shop", "bloat:public.audit_log"])
    fixed = runner.invoke(app, ["fixed", "shop", "stale_stats:public.legacy_imports"])
    listed = runner.invoke(app, ["findings", "shop"], terminal_width=200)

    assert acked.exit_code == fixed.exit_code == listed.exit_code == 0, listed.output
    assert re.search(r"bloat:public\.audit_log .*acknowledged", listed.output)
    assert re.search(r"stale_stats:public\.legacy_imports .*fixed", listed.output)
    unknown = runner.invoke(app, ["ack", "shop", "bloat:public.nope"])
    assert unknown.exit_code == 1 and "No Finding" in unknown.output


def test_compare_shows_what_changed_over_shared_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(SUPPORTED[-1]))
    runner = CliRunner()
    assert runner.invoke(app, ["connect", "shop"]).exit_code == 0
    assert runner.invoke(app, ["analyze", "shop"]).exit_code == 0
    assert runner.invoke(app, ["analyze", "shop", "--table", "public.tenants"]).exit_code == 0
    runs = runner.invoke(app, ["runs", "shop"], terminal_width=200)
    first, second = re.findall(r"\b([0-9a-f]{8})\b", runs.output)[:2]

    result = runner.invoke(app, ["compare", "shop", first, second], terminal_width=200)

    assert result.exit_code == 0, result.output
    assert "public.tenants" in result.output
    assert (
        "not compared" in result.output
        and "public.events" not in result.output.split("not compared")[0]
    )


def test_analyze_ranks_the_workload_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(SUPPORTED[-1]))
    runner = CliRunner()
    assert runner.invoke(app, ["connect", "shop"]).exit_code == 0

    result = runner.invoke(
        app,
        ["analyze", "shop", "-a", "workload", "--min-stats-window", "0"],
        terminal_width=200,
    )

    assert result.exit_code == 0, result.output
    assert "Most expensive" in result.output
    assert "slow_query:" in result.output
    assert "Largest of" not in result.output


def test_analyze_refuses_to_rank_young_statistics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(SUPPORTED[-1]))
    runner = CliRunner()
    assert runner.invoke(app, ["connect", "shop"]).exit_code == 0

    result = runner.invoke(
        app,
        ["analyze", "shop", "-a", "workload", "--min-stats-window", "87600"],
        terminal_width=200,
    )

    assert result.exit_code == 0, result.output
    assert "Warning:" in result.output and "minimum window" in result.output
    assert "Most expensive" not in result.output


def test_analyze_rejects_an_unknown_analyzer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(SUPPORTED[-1]))
    runner = CliRunner()
    assert runner.invoke(app, ["connect", "shop"]).exit_code == 0

    result = runner.invoke(app, ["analyze", "shop", "-a", "hotspot"])

    assert result.exit_code == 1
    assert "Unknown analyzer" in result.output
