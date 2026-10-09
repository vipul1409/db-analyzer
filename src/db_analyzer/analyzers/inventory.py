"""Inventory analyzer: store-agnostic findings from measured collection sizes."""

from db_analyzer.core.model import Observed, StorageStats
from db_analyzer.core.units import format_bytes

TOP_N = 10


def analyze(measured: list[StorageStats], top_n: int = TOP_N) -> list[Observed]:
    """The largest collections, as `size` Findings (facts worth reporting, not problems)."""
    total = sum(s.total_bytes for s in measured)
    ranked = sorted((s for s in measured if s.total_bytes > 0), key=lambda s: -s.total_bytes)
    return [
        Observed(
            category="size",
            subject=s.ref.qualified,
            severity="info",
            title=f"{s.ref.qualified} is #{rank} by size: {format_bytes(s.total_bytes)}",
            evidence={
                "rank": rank,
                "total_bytes": s.total_bytes,
                "data_bytes": s.data_bytes,
                "index_bytes": s.index_bytes,
                "toast_bytes": s.toast_bytes,
                "share_of_total": round(s.total_bytes / total, 4),
                "row_count": s.row_count,
                "row_count_method": s.row_count_method,
            },
        )
        for rank, s in enumerate(ranked[:top_n], start=1)
    ]
