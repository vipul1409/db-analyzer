"""All SQL goes through SafeExecutor. Only these modules may touch a database cursor directly:
the executor itself, and session setup (version check, read-only self-check, hardening)."""

import re
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "db_analyzer"
ALLOWED = {
    SRC / "safety" / "executor.py",
    SRC / "adapters" / "postgres" / "session.py",
}
RAW_DB_ACCESS = re.compile(r"\.(execute|executemany|cursor|copy)\(|psycopg\.connect\(")


def test_no_raw_database_access_outside_safe_executor() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}: {line.strip()}"
        for path in SRC.rglob("*.py")
        if path not in ALLOWED and "migrations" not in path.parts
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if RAW_DB_ACCESS.search(line) and "executor.execute(" not in line
    ]
    assert offenders == []


# The API is a thin adapter over AnalyzerService: it reaches the store, adapters, safety and
# agent graph only through the facade.
API = SRC / "api"
BEHIND_THE_FACADE = re.compile(
    r"^\s*(from|import)\s+db_analyzer\.(store|adapters|safety|agent\.(graph|tools|llm))\b",
    re.MULTILINE,
)


def test_the_api_reaches_the_domain_only_through_the_facade() -> None:
    offenders = [
        f"{path.relative_to(SRC)}: {m.group(0).strip()}"
        for path in API.rglob("*.py")
        for m in BEHIND_THE_FACADE.finditer(path.read_text())
    ]
    assert offenders == []
