"""The agent graph for LangGraph Studio (`langgraph.json`): `uv run langgraph dev`.

Set DBX_STUDIO_CONNECTION to the name of a Connection added with `dbx connect` (its DSN env
var must be set too).
"""

import os

from db_analyzer.service import AnalyzerService

_name = os.environ.get("DBX_STUDIO_CONNECTION")
if not _name:
    raise RuntimeError("set DBX_STUDIO_CONNECTION to a Connection name (see dbx connect)")
graph = AnalyzerService().studio_graph(_name)
