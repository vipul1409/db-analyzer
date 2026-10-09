"""PostgreSQL adapter."""

from db_analyzer.core.model import Capability

CAPABILITIES = frozenset({Capability.PROBE, Capability.STORAGE_STATS})
