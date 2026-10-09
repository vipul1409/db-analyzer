"""Postgres inventory collection: sizes and estimated rows from the catalog."""

from db_analyzer.adapters.postgres.queries import LIBRARY
from db_analyzer.adapters.sql_common.templates import run_template
from db_analyzer.core.model import CollectionKind, CollectionRef, StorageStats
from db_analyzer.safety.executor import SafeExecutor


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
        )
        for r in run_template(executor, template, purpose="inventory")
    ]
