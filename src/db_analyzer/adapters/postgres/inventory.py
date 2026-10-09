"""Postgres inventory collection: sizes, estimated rows and activity from the catalog; dead-tuple
scans and exact counts, which read table data, only where the EXPLAIN gate allows."""

from db_analyzer.adapters.postgres.queries import LIBRARY
from db_analyzer.adapters.sql_common.templates import run_template
from db_analyzer.core.model import (
    CollectionKind,
    CollectionRef,
    DeadTupleScan,
    IndexStats,
    Maintenance,
    StorageStats,
    qualify,
)
from db_analyzer.safety.executor import SafeExecutor

_DEAD_TUPLES = """
SELECT approx_tuple_count AS live_rows, dead_tuple_count AS dead_rows,
       dead_tuple_percent AS dead_percent, approx_free_percent AS free_percent
FROM {schema}.pgstattuple_approx({table}::regclass)
"""


def storage_stats(executor: SafeExecutor, server_version_num: int) -> list[StorageStats]:
    template = LIBRARY.get("storage_stats", server_version_num)
    return [
        StorageStats(
            ref=CollectionRef(str(r["schema"]), str(r["name"]), CollectionKind(r["kind"])),
            row_count=r["estimated_rows"],
            row_count_method="estimate",
            data_bytes=r["heap_bytes"],
            index_bytes=r["index_bytes"],
            toast_bytes=r["toast_bytes"],
            total_bytes=r["total_bytes"],
            partitions=r["partitions"],
            maintenance=Maintenance(
                live_rows=r["live_rows"],
                dead_rows=r["dead_rows"],
                modified_since_analyze=r["modified_since_analyze"],
                last_vacuum=r["last_vacuum"],
                last_analyze=r["last_analyze"],
                autovacuum_disabled=bool(r["autovacuum_disabled"]),
            ),
        )
        for r in run_template(executor, template, purpose="inventory")
    ]


def index_stats(executor: SafeExecutor, server_version_num: int) -> list[IndexStats]:
    template = LIBRARY.get("index_stats", server_version_num)
    return [
        IndexStats(
            name=qualify(str(r["schema"]), str(r["name"])),
            table=CollectionRef(
                str(r["table_schema"]), str(r["table_name"]), CollectionKind(r["table_kind"])
            ),
            method=str(r["method"]),
            keys=list(r["keys"]),
            columns=list(r["columns"]),
            include=list(r["include"]),
            predicate=r["predicate"],
            index_bytes=int(r["index_bytes"]),
            scans=int(r["scans"]),
            unique=bool(r["is_unique"]),
            primary=bool(r["is_primary"]),
            constraint=bool(r["backs_constraint"]),
            valid=bool(r["is_valid"]),
            partitioned=bool(r["is_partitioned"]),
            nulls_not_distinct=bool(r["nulls_not_distinct"]),
        )
        for r in run_template(executor, template, purpose="index health")
    ]


def scan_dead_tuples(
    executor: SafeExecutor, ref: CollectionRef, pgstattuple_schema: str
) -> DeadTupleScan:
    """pgstattuple_approx: reads the pages the visibility map does not mark all-visible."""
    sql = _DEAD_TUPLES.format(schema=_ident(pgstattuple_schema), table=_literal(ref.qualified))
    [r] = executor.execute(sql, purpose="dead tuples")
    return DeadTupleScan(
        live_rows=int(r["live_rows"]),
        dead_rows=int(r["dead_rows"]),
        dead_percent=float(r["dead_percent"]),
        free_percent=float(r["free_percent"]),
    )


def count_exactly(executor: SafeExecutor, ref: CollectionRef) -> int:
    [r] = executor.execute(f"SELECT count(*) AS n FROM {ref.qualified}", purpose="exact count")
    return int(r["n"])


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"
