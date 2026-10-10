"""PostgreSQL adapter."""

from db_analyzer.core.model import Capability

CAPABILITIES = frozenset(
    {
        Capability.PROBE,
        Capability.STORAGE_STATS,
        Capability.EXACT_COUNTS,
        Capability.READONLY_SQL,
        Capability.WORKLOAD,
    }
)
