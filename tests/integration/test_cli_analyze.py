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
