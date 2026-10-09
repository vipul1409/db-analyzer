from pathlib import Path

import pytest

from db_analyzer.adapters.sql_common.templates import TemplateLibrary


def write(directory: Path, filename: str, min_version: int, body: str = "SELECT 1 AS a") -> None:
    (directory / filename).write_text(
        f"""-- name: sizes
-- columns: a
-- min_version: {min_version}
-- privilege: catalog only
{body}
"""
    )


def test_header_is_parsed(tmp_path: Path) -> None:
    (tmp_path / "sizes.sql").write_text(
        """-- name: sizes
-- columns: schema, name, total_bytes
-- min_version: 15
-- privilege: pg_monitor
SELECT 1
"""
    )

    t = TemplateLibrary(tmp_path).get("sizes", server_version_num=150004)

    assert t.columns == ("schema", "name", "total_bytes")
    assert t.min_version == 15
    assert t.privilege == "pg_monitor"
    assert t.sql.strip() == "SELECT 1"


def test_newest_variant_the_server_supports_is_chosen(tmp_path: Path) -> None:
    write(tmp_path, "sizes.sql", 15, "SELECT 15 AS a")
    write(tmp_path, "sizes.pg17.sql", 17, "SELECT 17 AS a")
    library = TemplateLibrary(tmp_path)

    assert "15" in library.get("sizes", server_version_num=160002).sql
    assert "17" in library.get("sizes", server_version_num=170000).sql
    assert "17" in library.get("sizes", server_version_num=180001).sql


def test_server_older_than_every_variant_is_refused(tmp_path: Path) -> None:
    write(tmp_path, "sizes.sql", 16)

    with pytest.raises(LookupError, match="needs PostgreSQL 16"):
        TemplateLibrary(tmp_path).get("sizes", server_version_num=150000)


@pytest.mark.parametrize("missing", ["name", "columns", "min_version", "privilege"])
def test_template_without_full_header_is_refused(tmp_path: Path, missing: str) -> None:
    header = {"name": "x", "columns": "a", "min_version": "15", "privilege": "none"}
    del header[missing]
    (tmp_path / "x.sql").write_text(
        "".join(f"-- {k}: {v}\n" for k, v in header.items()) + "SELECT 1"
    )

    with pytest.raises(ValueError, match=missing):
        TemplateLibrary(tmp_path)


def test_postgres_library_loads() -> None:
    from db_analyzer.adapters.postgres.queries import LIBRARY

    assert LIBRARY.get("storage_stats", server_version_num=150000).columns
