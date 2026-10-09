"""`dbx chat` end to end with a cassette (see test_agent_chat.py for re-recording)."""

import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from db_analyzer.cli import app

from .conftest import SUPPORTED, seeded_dsn
from .test_agent_chat import CASSETTES, RECORDED_ON, RECORDING

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(RECORDED_ON not in SUPPORTED, reason=f"cassettes need PG {RECORDED_ON}"),
]


def test_chat_streams_the_answer_with_tool_and_sql_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cassette = CASSETTES / "test_cli_chat"
    if RECORDING:
        shutil.rmtree(cassette, ignore_errors=True)
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DBX_HOME", str(tmp_path))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(RECORDED_ON))
    monkeypatch.setenv("DBX_LLM_CASSETTE", str(cassette))
    monkeypatch.setenv("DBX_LLM_CASSETTE_MODE", os.environ.get("DBX_LLM_CASSETTE_MODE", ""))
    runner = CliRunner()
    assert runner.invoke(app, ["connect", "shop"]).exit_code == 0

    result = runner.invoke(app, ["chat", "shop"], input="What are the biggest tables?\n\n")

    assert result.exit_code == 0, result.output
    assert "Thread " in result.output
    assert "→ get_storage_stats" in result.output
    assert "sql inventory" in result.output
    assert "public.events" in result.output
    assert "out tokens" in result.output


def test_chat_refuses_a_thread_from_another_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBX_HOME", str(tmp_path))
    monkeypatch.setenv("DBX_DSN", seeded_dsn(RECORDED_ON))
    runner = CliRunner()
    runner.invoke(app, ["connect", "a"])
    runner.invoke(app, ["connect", "b"])
    from db_analyzer.service import AnalyzerService

    service = AnalyzerService(home=tmp_path)
    thread = service.start_thread(service.connection("a").id)

    result = runner.invoke(app, ["chat", "b", "--thread", thread.id])

    assert result.exit_code == 1
    assert "belongs to another Connection" in result.output
