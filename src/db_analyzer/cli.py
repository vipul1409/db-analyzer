"""`dbx` command line. A thin layer over AnalyzerService."""

from datetime import UTC, datetime
from typing import Annotated

import psycopg
import typer
from rich.console import Console
from rich.table import Table

from db_analyzer.core.model import ConnectionRefused, ProbeResult
from db_analyzer.service import AnalyzerService

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

WANTED_EXTENSIONS = ("pg_stat_statements", "hypopg", "pgstattuple", "pg_buffercache")


@app.callback()
def main() -> None:
    """DB Analyzer: read-only database analysis."""


@app.command()
def connect(
    name: Annotated[str, typer.Argument(help="Name for this Connection.")],
    dsn_env: Annotated[
        str, typer.Option(help="Environment variable holding the DSN. The DSN is never stored.")
    ] = "DBX_DSN",
) -> None:
    """Add (or update) a Connection to one database and show what the analyzer can see."""
    service = AnalyzerService()
    connection = service.add_connection(name=name, dsn_env=dsn_env)
    try:
        probe = service.probe(connection.id)
    except ConnectionRefused as e:
        console.print(f"[bold red]Refused:[/] {e}")
        raise typer.Exit(1) from e
    except psycopg.OperationalError as e:
        console.print(f"[bold red]Cannot connect:[/] {str(e).strip()}")
        raise typer.Exit(1) from e
    except psycopg.Error as e:
        console.print(f"[bold red]Probe failed:[/] {str(e).strip()}")
        raise typer.Exit(1) from e
    _render_probe(name, probe)


def _render_probe(name: str, p: ProbeResult) -> None:
    if not p.in_recovery:
        console.print("[bold black on yellow] Connected to PRIMARY [/]")
    table = Table(title=f"What I can see on '{name}'", show_header=False)
    table.add_column(style="bold")
    table.add_column()
    host = "Azure Flexible Server" if p.host_type == "azure_flexible" else "self-managed"
    role = "replica" if p.in_recovery else "primary"
    table.add_row("Server", f"PostgreSQL {p.server_version} ({host}, {role})")
    table.add_row(
        "Extensions",
        ", ".join(
            f"[green]{e}[/]" if e in p.extensions else f"[dim]{e} (not installed)[/]"
            for e in WANTED_EXTENSIONS
        ),
    )
    priv = p.privileges
    table.add_row("pg_monitor", "yes" if priv.pg_monitor else "[red]no[/]")
    unreadable = (
        f", [yellow]{len(priv.unreadable_tables)} without SELECT[/]: "
        + ", ".join(priv.unreadable_tables[:5])
        + (" …" if len(priv.unreadable_tables) > 5 else "")
        if priv.unreadable_tables
        else ""
    )
    table.add_row("Tables", f"{priv.readable_tables} readable{unreadable}")
    table.add_row("Statistics reset", _age(p.stats.database_stats_reset))
    if "pg_stat_statements" in p.extensions:
        table.add_row("Statement stats reset", _age(p.stats.statements_stats_reset))
    table.add_row(
        "Never analyzed tables",
        str(p.stats.never_analyzed_tables) if p.stats.never_analyzed_tables else "none",
    )
    console.print(table)


def _age(at: datetime | None) -> str:
    if at is None:
        return "never"
    hours = (datetime.now(UTC) - at).total_seconds() / 3600
    return f"{hours:.1f} h ago" if hours < 48 else f"{hours / 24:.0f} days ago"
