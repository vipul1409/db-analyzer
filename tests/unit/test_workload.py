from datetime import UTC, datetime, timedelta

from db_analyzer.adapters.postgres import workload as pg_workload
from db_analyzer.analyzers import plan_rules, workload
from db_analyzer.core.model import (
    CollectionKind,
    CollectionRef,
    Observed,
    PlanNode,
    RelationEstimate,
    ScanActivity,
    StatementPlan,
    UnindexedForeignKey,
    WorkloadReport,
    WorkloadStatement,
)

NOW = datetime(2026, 10, 9, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=3)
BOOKINGS = CollectionRef("public", "bookings", CollectionKind.TABLE)


def statement(text: str, calls: int = 10, total_ms: float = 100.0, **kw: int) -> WorkloadStatement:
    return WorkloadStatement(
        text=text,
        calls=calls,
        total_ms=total_ms,
        rows=kw.get("rows", 0),
        shared_blks_read=kw.get("shared_blks_read", 0),
        temp_blks_written=kw.get("temp_blks_written", 0),
    )


def rank(*statements: WorkloadStatement, **kw: object) -> WorkloadReport:
    args: dict[str, object] = {"stats_reset": LONG_AGO, **kw}
    return workload.report(
        list(statements),
        source="pg_stat_statements",
        now=NOW,
        **args,  # type: ignore[arg-type]
    )


def test_the_same_statement_has_the_same_fingerprint_whatever_the_formatting() -> None:
    a = workload.fingerprint("SELECT id FROM bookings WHERE account_id = $1")
    b = workload.fingerprint("SELECT id\n  FROM bookings   WHERE account_id = $1;")

    assert a == b
    assert a != workload.fingerprint("SELECT id FROM bookings WHERE tenant_id = $1")


def test_rows_of_one_statement_from_several_users_are_merged() -> None:
    sql = "SELECT 1 FROM bookings WHERE id = $1"

    r = rank(statement(sql, calls=10, total_ms=50), statement(sql, calls=30, total_ms=150))

    [item] = r.items
    assert (item.calls, item.total_ms, item.mean_ms) == (40, 200.0, 5.0)


def test_statements_are_ranked_by_total_time() -> None:
    r = rank(
        statement("SELECT 1 FROM a WHERE x = $1", total_ms=10),
        statement("SELECT 1 FROM b WHERE x = $1", total_ms=900),
        statement("SELECT 1 FROM c WHERE x = $1", total_ms=90),
    )

    assert [i.text[14] for i in r.items] == ["b", "c", "a"]
    assert r.items[0].share_of_time == 0.9


def test_a_statement_that_is_top_by_another_measure_is_kept_and_says_why() -> None:
    spill = statement("SELECT x FROM a ORDER BY y", total_ms=1, temp_blks_written=500)
    cheap = statement("SELECT 1 FROM b WHERE x = $1", total_ms=1)

    r = rank(spill, cheap, top_n=1)

    by_text = {i.text: i for i in r.items}
    assert "temp_blocks_written" in by_text[spill.text].ranked_by
    assert by_text[spill.text].temp_blks_written == 500


def test_each_ranking_keeps_its_own_top_n() -> None:
    slow = statement("SELECT 1 FROM a WHERE x = $1", total_ms=900)
    reader = statement("SELECT 1 FROM b WHERE x = $1", total_ms=1, shared_blks_read=7)

    r = rank(slow, reader, top_n=1)

    assert {i.text for i in r.items} == {slow.text, reader.text}


def test_statements_that_are_not_queries_or_dml_are_left_out_and_counted() -> None:
    r = rank(
        statement("SELECT 1 FROM a WHERE x = $1"),
        statement("CREATE ROLE app PASSWORD 'hunter2'"),
        statement("SET search_path = 'x'"),
        statement("<insufficient privilege>"),
        statement("SELECT * FROM a WHERE x IN ($1, $2, $"),
    )

    assert [i.text for i in r.items] == ["SELECT 1 FROM a WHERE x = $1"]
    assert r.excluded == {
        "utility statement": 2,
        "text hidden (no pg_read_all_stats)": 1,
        "text unparseable (truncated?)": 1,
    }
    assert any("pg_read_all_stats" in w for w in r.warnings)
    assert "hunter2" not in repr(r)


def test_dml_statements_are_ranked() -> None:
    r = rank(
        statement("UPDATE bookings SET amount = amount WHERE tenant_id = $1 AND status = $2"),
        statement("DELETE FROM bookings WHERE id = $1"),
        statement("INSERT INTO bookings (id) VALUES ($1)"),
    )

    assert len(r.items) == 3


def test_a_recent_stats_reset_warns_and_ranks_nothing() -> None:
    r = rank(statement("SELECT 1 FROM a WHERE x = $1"), stats_reset=NOW - timedelta(minutes=5))

    assert r.items == []
    assert r.refused is not None and "5 min" in r.refused
    assert r.warnings[0] == r.refused
    assert workload.analyze(r) == []


def test_the_minimum_window_is_configurable() -> None:
    reset = NOW - timedelta(minutes=5)

    r = rank(
        statement("SELECT 1 FROM a WHERE x = $1"),
        stats_reset=reset,
        min_window=timedelta(minutes=1),
    )

    assert r.refused is None and len(r.items) == 1


def test_statistics_under_a_day_old_rank_with_a_warning() -> None:
    r = rank(statement("SELECT 1 FROM a WHERE x = $1"), stats_reset=NOW - timedelta(hours=3))

    assert len(r.items) == 1
    assert any("3.0 h" in w for w in r.warnings)


def test_statistics_read_on_a_replica_say_so() -> None:
    r = rank(statement("SELECT 1 FROM a WHERE x = $1"), on_replica=True)

    assert any("replica" in w for w in r.warnings)


def findings(r: WorkloadReport) -> dict[str, Observed]:
    return {o.fingerprint: o for o in workload.analyze(r)}


def test_slow_query_findings_are_fingerprinted_by_the_text_hash() -> None:
    sql = "SELECT id FROM bookings WHERE account_id = $1"

    found = findings(rank(statement(sql, calls=20, total_ms=400, rows=20)))

    f = found[f"slow_query:{workload.fingerprint(sql)}"]
    assert f.category == "slow_query"
    assert f.collection is None
    assert f.evidence["text"] == sql
    assert f.evidence["calls"] == 20
    assert f.evidence["mean_ms"] == 20.0


def test_severity_follows_the_share_of_the_workload() -> None:
    r = rank(
        statement("SELECT 1 FROM a WHERE x = $1", total_ms=700),
        statement("SELECT 1 FROM b WHERE x = $1", total_ms=200),
        statement("SELECT 1 FROM c WHERE x = $1", total_ms=100),
    )

    assert [o.severity for o in workload.analyze(r)] == ["high", "medium", "medium"]


def test_a_statement_that_spills_to_disk_gets_advice() -> None:
    sql = "SELECT x FROM a ORDER BY y"

    f = findings(rank(statement(sql, temp_blks_written=1280)))[
        f"slow_query:{workload.fingerprint(sql)}"
    ]

    assert "10.0 MB of temporary files" in (f.recommendation or "")


SEQ_SCAN = PlanNode(
    "Seq Scan",
    rows=10,
    width=19,
    total_cost=542.17,
    relation="public.bookings",
    filter="(bookings.account_id = $1)",
    filter_columns=["account_id"],
)
SHOP = plan_rules.PlanContext(
    relations={"public.bookings": RelationEstimate("public.bookings", 20_000, 20_000, 0, True)}
)


def planned(sql: str, plan: StatementPlan, **kw: int) -> Observed:
    r = rank(statement(sql, **kw))
    found = workload.analyze(r, {workload.fingerprint(sql): plan}, SHOP)
    return {o.fingerprint: o for o in found}[f"slow_query:{workload.fingerprint(sql)}"]


def test_a_planned_statement_says_why_it_is_slow_with_the_plan_as_evidence() -> None:
    sql = "SELECT id FROM bookings WHERE account_id = $1"

    f = planned(sql, StatementPlan(SEQ_SCAN), shared_blks_read=900)

    assert f.evidence["plan"] == [SEQ_SCAN.line()]
    [reason] = f.evidence["plan_rules"]
    assert reason["rule"] == "seq_scan_selective_filter"
    assert reason["node"] == SEQ_SCAN.line()
    assert f.recommendation is not None and reason["explanation"] in f.recommendation
    # The plan explains it: the generic hint about blocks read gives way.
    assert "outside shared buffers" not in f.recommendation


def test_dml_planned_as_its_row_lookup_says_the_plan_covers_only_that() -> None:
    sql = "UPDATE bookings SET amount = amount WHERE account_id = $1"

    f = planned(sql, StatementPlan(SEQ_SCAN, row_lookup_only=True))

    assert f.evidence["row_lookup_only"] is True
    assert (f.recommendation or "").startswith("The plan covers only how it finds its rows")


def test_a_statement_that_could_not_be_planned_says_why_and_keeps_its_hints() -> None:
    sql = "SELECT x FROM a ORDER BY y"

    f = planned(
        sql,
        StatementPlan(None, skipped="refused: function foo is not allowed"),
        temp_blks_written=1280,
    )

    assert f.evidence["plan_skipped"] == "refused: function foo is not allowed"
    assert "plan" not in f.evidence
    assert "10.0 MB of temporary files" in (f.recommendation or "")


def test_no_source_gives_steps_to_enable_pg_stat_statements() -> None:
    steps = pg_workload.enable_steps(installed=False, preloaded=False, azure=False)

    text = " ".join(steps)
    assert "shared_preload_libraries" in text and "restart" in text
    assert "CREATE EXTENSION pg_stat_statements" in text
    assert "pg_read_all_stats" in text
    r = workload.no_source(steps)
    assert r.source is None and r.enable_steps == steps


def test_steps_skip_what_is_already_done() -> None:
    steps = " ".join(pg_workload.enable_steps(installed=False, preloaded=True, azure=False))

    assert "shared_preload_libraries" not in steps
    assert "CREATE EXTENSION" in steps


def fk(table: str, *columns: str) -> UnindexedForeignKey:
    return UnindexedForeignKey(
        CollectionRef("public", table, CollectionKind.TABLE), f"{table}_fkey", list(columns)
    )


def scans(table: str, seq: int, idx: int, rows: int = 100_000) -> ScanActivity:
    return ScanActivity(
        CollectionRef("public", table, CollectionKind.TABLE), seq, seq * rows, idx, rows
    )


def test_foreign_key_without_an_index_is_a_missing_index_finding_with_ddl() -> None:
    [f] = workload.schema_only_review([fk("bookings", "account_id")], [])

    assert f.fingerprint == "missing_index:public.bookings(account_id)"
    assert f.collection == "public.bookings"
    assert f.ddl == "CREATE INDEX CONCURRENTLY ON public.bookings (account_id);"
    assert f.severity == "low"


def test_a_composite_foreign_key_lists_its_columns_in_order() -> None:
    [f] = workload.schema_only_review([fk("bookings", "tenant_id", "Account Id")], [])

    assert f.subject == "public.bookings(tenant_id,Account Id)"
    assert f.ddl == 'CREATE INDEX CONCURRENTLY ON public.bookings (tenant_id, "Account Id");'


def test_a_table_read_mostly_by_sequential_scans_is_reported() -> None:
    found = workload.schema_only_review([], [scans("events", seq=400, idx=3)])

    [f] = found
    assert f.fingerprint == "missing_index:public.events:seq_scan_heavy"
    assert f.evidence["seq_scans"] == 400
    assert f.severity == "medium"


def test_small_or_index_served_tables_are_not_seq_scan_heavy() -> None:
    found = workload.schema_only_review(
        [],
        [
            scans("tiny", seq=900, idx=0, rows=50),
            scans("indexed", seq=60, idx=5_000),
            scans("rarely", seq=3, idx=0),
        ],
    )

    assert found == []


def test_a_foreign_key_on_a_seq_scan_heavy_table_ranks_higher() -> None:
    found = workload.schema_only_review(
        [fk("bookings", "account_id"), fk("audit_log", "tenant_id")],
        [scans("bookings", seq=500, idx=1)],
    )

    severities = {o.fingerprint: o.severity for o in found}
    assert severities["missing_index:public.bookings(account_id)"] == "medium"
    assert severities["missing_index:public.audit_log(tenant_id)"] == "low"
    assert found[-1].fingerprint == "missing_index:public.audit_log(tenant_id)"
