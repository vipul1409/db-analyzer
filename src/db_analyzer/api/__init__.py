"""The HTTP API over AnalyzerService (ADR 0015), served by `dbx serve`."""

from db_analyzer.api.app import create_app

__all__ = ["create_app"]
