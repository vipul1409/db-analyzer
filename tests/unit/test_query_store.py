from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

from db_analyzer.adapters.postgres import workload as pg_workload
from db_analyzer.analyzers import workload
from db_analyzer.core.model import Privileges, ProbeResult, StatsFreshness
from db_analyzer.safety.executor import Row, SafeExecutor

NOW = datetime(2026, 10, 9, tzinfo=UTC)
QUERY_STORE = pg_workload.QUERY_STORE


def probe(
    host: str = "azure_flexible", capture: str | None = "top", azure_sys: bool = True
) -> ProbeResult:
    return ProbeResult(
        server_version_num=170000,
        server_version="17.0",
        in_recovery=False,
        host_type=host,  # type: ignore[arg-type]
        extensions={},
        privileges=Privileges(True, True, 0, [], azure_sys_connect=azure_sys),
        settings={"pg_qs.query_capture_mode": capture},
        stats=StatsFreshness(None, None, 0, None),
        taken_at=NOW,
    )


# --- Values replaced by placeholders ----------------------------------------------------------


def test_literals_become_placeholders_numbered_after_the_existing_ones() -> None:
    text = "SELECT * FROM t WHERE a = 'alice@example.com' AND b = $1 AND c IN (1, 2.5) LIMIT 10"

    assert pg_workload.replace_constants(text) == (
        "SELECT * FROM t WHERE a = $2 AND b = $1 AND c IN ($3, $4) LIMIT $5"
    )


def test_a_negative_number_is_one_value_but_a_subtraction_keeps_its_operator() -> None:
    assert pg_workload.replace_constants("SELECT a - 1 FROM t WHERE b = -3 OR c = (-4)") == (
        "SELECT a - $1 FROM t WHERE b = $2 OR c = ($3)"
    )


def test_every_kind_of_string_literal_is_replaced() -> None:
    text = "SELECT 1 FROM t WHERE a = E'x\\'y' AND b = B'101' AND c = $$secret$$ AND d = 'é'"

    assert pg_workload.replace_constants(text) == (
        "SELECT $1 FROM t WHERE a = $2 AND b = $3 AND c = $4 AND d = $5"
    )


def test_text_that_cannot_be_scanned_is_dropped_so_no_value_leaks() -> None:
    assert pg_workload.replace_constants("SELECT 1 FROM t WHERE a = 'truncated secr") is None


def test_text_without_values_is_unchanged() -> None:
    sql = "SELECT id FROM bookings WHERE account_id = $1"

    assert pg_workload.replace_constants(sql) == sql


# --- When Query Store is readable -------------------------------------------------------------


def test_query_store_is_readable_on_azure_with_capture_on_and_connect_on_azure_sys() -> None:
    assert QUERY_STORE.name == "azure_query_store"
    assert QUERY_STORE.enable_steps(probe()) == []


def test_query_store_exists_only_on_azure() -> None:
    [step] = QUERY_STORE.enable_steps(probe(host="self_managed", capture=None, azure_sys=False))

    assert "Azure" in step


def test_query_store_steps_say_what_is_missing() -> None:
    off = " ".join(QUERY_STORE.enable_steps(probe(capture="none")))
    unset = " ".join(QUERY_STORE.enable_steps(probe(capture=None)))
    no_connect = " ".join(QUERY_STORE.enable_steps(probe(azure_sys=False)))

    assert "pg_qs.query_capture_mode" in off and "pg_qs.query_capture_mode" in unset
    assert "azure_sys" not in off
    assert "GRANT CONNECT ON DATABASE azure_sys" in no_connect
    assert "pg_qs.query_capture_mode" not in no_connect


# --- Reading it through an auxiliary session --------------------------------------------------


@dataclass
class FakeExecutor:
    """The Connection's executor: answers the database oid, and opens an auxiliary session whose
    executor answers the Query Store read."""

    rows: list[Row]
    opened: list[str] = field(default_factory=list)
    sql: list[str] = field(default_factory=list)

    def execute(self, sql: str, purpose: str) -> list[Row]:
        self.sql.append(sql)
        return [{"oid": 16384}]

    @contextmanager
    def auxiliary(self, database: str) -> Iterator[Any]:
        self.opened.append(database)
        outer = self

        class Aux:
            def execute(self, sql: str, purpose: str) -> list[Row]:
                outer.sql.append(sql)
                return outer.rows

        yield Aux()


def qs_row(text: str, since: datetime, **kw: Any) -> Row:
    return {
        "query": text,
        "calls": kw.get("calls", 3),
        "total_exec_time": kw.get("total_time", 30.0),
        "rows": 3,
        "shared_blks_read": 7,
        "temp_blks_written": 0,
        "since": since.replace(tzinfo=None),  # Query Store keeps UTC without a time zone
    }


def test_query_store_is_read_through_an_auxiliary_session_to_azure_sys() -> None:
    older, newer = datetime(2026, 10, 2, 8, 0), datetime(2026, 10, 5, 8, 0)
    executor = FakeExecutor(
        [
            qs_row("SELECT * FROM users WHERE email = 'bob@example.com'", newer),
            qs_row("SELECT * FROM bookings WHERE id = $1", older, calls=5, total_time=50.0),
        ]
    )

    reading = QUERY_STORE.read(cast(SafeExecutor, executor), probe())

    assert executor.opened == ["azure_sys"]
    assert "db_id = 16384" in executor.sql[-1]
    assert [s.text for s in reading.statements] == [
        "SELECT * FROM users WHERE email = $1",
        "SELECT * FROM bookings WHERE id = $1",
    ]
    assert (reading.statements[1].calls, reading.statements[1].total_ms) == (5, 50.0)
    assert reading.stats_reset == older.replace(tzinfo=UTC)


def test_a_statement_whose_values_cannot_be_replaced_is_counted_but_never_read() -> None:
    since = datetime(2026, 10, 2)
    executor = FakeExecutor([qs_row("SELECT 1 FROM t WHERE a = 'cut off", since)])

    reading = QUERY_STORE.read(cast(SafeExecutor, executor), probe())

    [s] = reading.statements
    assert "cut off" not in s.text
    assert s.text == pg_workload.UNREADABLE_TEXT
    ranked = workload.report(reading.statements, source=QUERY_STORE.name, stats_reset=None, now=NOW)
    assert ranked.excluded == {workload.UNPARSEABLE_TEXT: 1}


def test_an_empty_query_store_has_no_window() -> None:
    reading = QUERY_STORE.read(cast(SafeExecutor, FakeExecutor([])), probe())

    assert reading.statements == [] and reading.stats_reset is None


def test_text_cut_off_at_the_maximum_length_is_left_out_even_when_it_still_parses() -> None:
    since = datetime(2026, 10, 2)
    cut = "SELECT a, b FROM t"  # was: ... WHERE x = 1, cut at pg_qs.max_query_text_length
    executor = FakeExecutor([qs_row(cut, since), qs_row("SELECT a FROM t", since)])
    limited = replace(probe(), settings={**probe().settings, "pg_qs.max_query_text_length": "18"})

    reading = QUERY_STORE.read(cast(SafeExecutor, executor), limited)

    assert [s.text for s in reading.statements] == [pg_workload.UNREADABLE_TEXT, "SELECT a FROM t"]
