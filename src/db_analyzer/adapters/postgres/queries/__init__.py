"""Postgres catalog-query templates (proposal §5.6)."""

from pathlib import Path

from db_analyzer.adapters.sql_common.templates import TemplateLibrary

LIBRARY = TemplateLibrary(Path(__file__).parent)
