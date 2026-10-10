"""`dbx serve`: where it listens, and when it refuses to."""

from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

from db_analyzer.cli import app


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    """What uvicorn was asked to serve; nothing listens."""
    monkeypatch.setenv("DBX_HOME", str(tmp_path))
    monkeypatch.delenv("DBX_API_TOKEN", raising=False)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    return calls


def test_serves_on_loopback_by_default(served: list[dict[str, Any]]) -> None:
    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert served == [{"host": "127.0.0.1", "port": 8765}]


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::", "myhost.local"])
def test_refuses_another_host_without_a_token(served: list[dict[str, Any]], host: str) -> None:
    result = CliRunner().invoke(app, ["serve", "--host", host])

    assert result.exit_code == 1
    assert "DBX_API_TOKEN" in result.output
    assert served == []


def test_serves_another_host_with_a_token(
    served: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBX_API_TOKEN", "s3cret")

    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0", "--port", "9000"])

    assert result.exit_code == 0, result.output
    assert served == [{"host": "0.0.0.0", "port": 9000}]


@pytest.mark.parametrize("host", ["localhost", "::1", "127.0.0.2"])
def test_any_loopback_host_needs_no_token(served: list[dict[str, Any]], host: str) -> None:
    assert CliRunner().invoke(app, ["serve", "--host", host]).exit_code == 0
