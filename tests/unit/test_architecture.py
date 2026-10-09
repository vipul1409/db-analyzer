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
