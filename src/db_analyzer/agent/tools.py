"""Tool factory: one agent tool per capability the Connection has.

Tools are thin: they call a TurnBackend (bound by the service to one Connection, Thread and
turn) and return compact, pre-digested JSON (proposal §6.2), never raw catalog rows.
"""

import json
from collections.abc import Callable
from typing import Any, Protocol

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from db_analyzer.core.model import Capability


class TurnBackend(Protocol):
    def probe(self) -> dict[str, Any]: ...

    def storage(self, top_n: int) -> dict[str, Any]: ...


class NoArgs(BaseModel):
    pass


class StorageArgs(BaseModel):
    top_n: int = Field(ge=1, le=50, description="How many collections to list, largest first")


def _probe(backend: TurnBackend) -> BaseTool:
    return StructuredTool.from_function(
        func=lambda: json.dumps(backend.probe()),
        name="probe",
        description=(
            "What the analyzer can see on this database: version, primary or replica, "
            "extensions, privileges and statistics freshness."
        ),
        args_schema=NoArgs,
    )


def _storage(backend: TurnBackend) -> BaseTool:
    return StructuredTool.from_function(
        func=lambda top_n: json.dumps(backend.storage(top_n)),
        name="get_storage_stats",
        description=(
            "Measure every table's size (heap, index, TOAST, total) and estimated rows from the "
            "catalog, recorded as an inventory Run. Returns the largest top_n, largest first."
        ),
        args_schema=StorageArgs,
    )


_FACTORIES: dict[Capability, Callable[[TurnBackend], BaseTool]] = {
    Capability.PROBE: _probe,
    Capability.STORAGE_STATS: _storage,
}


def build_tools(backend: TurnBackend, capabilities: frozenset[Capability]) -> list[BaseTool]:
    return [factory(backend) for cap, factory in _FACTORIES.items() if cap in capabilities]
