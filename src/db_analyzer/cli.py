"""`dbx` command line. A thin layer over AnalyzerService."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import psycopg
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from db_analyzer.agent.events import AgentEvent
from db_analyzer.agent.llm import LLMConfigError
from db_analyzer.core.model import ConnectionRefused, ProbeResult, QueryRejected, Run
from db_analyzer.core.units import format_bytes
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
    with _database_errors("Probe failed"):
        probe = service.probe(connection.id)
    _render_probe(name, probe)


@app.command()
def analyze(
    name: Annotated[str, typer.Argument(help="Connection to analyse (see `dbx connect`).")],
    report: Annotated[
        Path | None, typer.Option(help="Write a Markdown report of the Run to this file.")
    ] = None,
) -> None:
    """Run the deterministic inventory analysis (no LLM) and show tables by size."""
    service = AnalyzerService()
    try:
        connection = service.connection(name)
    except KeyError as e:
        console.print(f"[bold red]No Connection named {name!r}.[/] Add it with `dbx connect`.")
        raise typer.Exit(1) from e
    with _database_errors("Analysis failed"):
        run = service.run(connection.id, analyzers=["inventory"])
    _render_run(service, run)
    if report is not None:
        report.write_bytes(service.export(run.id, "md"))
        console.print(f"Report written to [bold]{report}[/]")


@app.command()
def chat(
    name: Annotated[str, typer.Argument(help="Connection to talk about (see `dbx connect`).")],
    thread: Annotated[
        str | None, typer.Option(help="Resume this Thread instead of starting a new one.")
    ] = None,
) -> None:
    """Chat with the agent about one Connection. Needs OPENAI_API_KEY."""
    service = AnalyzerService()
    try:
        connection = service.connection(name)
        current = service.thread(thread) if thread else service.start_thread(connection.id)
    except KeyError as e:
        console.print(f"[bold red]{e.args[0]}[/]")
        raise typer.Exit(1) from e
    if current.connection_id != connection.id:
        console.print(f"[bold red]Thread {thread} belongs to another Connection.[/]")
        raise typer.Exit(1)
    console.print(
        f"[dim]Thread {current.id} on '{name}'. Resume with "
        f"`dbx chat {name} --thread {current.id}`. Empty line or Ctrl-D quits.[/]"
    )
    while True:
        try:
            message = console.input("[bold]you>[/] ")
        except EOFError:
            break
        if not message.strip():
            break
        try:
            asyncio.run(_chat_turn(service, current.id, message))
        except LLMConfigError as e:
            console.print(f"[bold red]Cannot reach the model:[/] {e}")
            raise typer.Exit(1) from e


async def _chat_turn(service: AnalyzerService, thread_id: str, message: str) -> None:
    renderer = TurnRenderer(console)
    async for event in service.send(thread_id, message):
        renderer.render(event)


class TurnRenderer:
    """Renders one Turn's AgentEvents: the answer streams; tools, SQL and limits go between."""

    SQL_CHARS = 90

    def __init__(self, out: Console):
        self._out = out
        self._mid_line = False

    def render(self, event: AgentEvent) -> None:
        match event.type:
            case "token":
                self._out.print(event.text, end="", markup=False, highlight=False)
                self._mid_line = True
            case "tool_started":
                args = ", ".join(f"{k}={v!r}" for k, v in event.args.items())
                self._line(f"[cyan]→ {escape(event.name)}({escape(args)})[/]")
            case "tool_finished" if not event.ok:
                self._line(f"[yellow]  ✗ {escape(event.name)} failed: {escape(event.summary)}[/]")
            case "sql_executed":
                cost = f", cost {event.plan_cost:.3g}" if event.plan_cost is not None else ""
                stats = f"{event.row_count} rows, {event.duration_ms or 0:.0f} ms{cost}"
                self._line(f"[dim]  sql {event.purpose} ({stats}): {self._sql(event.sql)}[/]")
            case "sql_rejected":
                self._line(f"[red]  sql rejected ({event.reason}): {self._sql(event.sql)}[/]")
            case "run_finished":
                self._line(f"[dim]  Run {event.run_id[:8]} {event.status}[/]")
            case "limit_reached":
                self._line(
                    f"[yellow]Stopped at the {event.limit} limit ({event.used} of {event.max}).[/]"
                )
            case "error":
                self._line(f"[bold red]Error:[/] {escape(event.message)}", wrap=True)
            case "usage":
                cost = f" · ${event.cost_usd:.4f}" if event.cost_usd is not None else ""
                self._line(
                    f"[dim]{event.input_tokens:,} in ({event.cached_tokens:,} cached) / "
                    f"{event.output_tokens:,} out tokens{cost}[/]"
                )
            case "done":
                if self._mid_line:
                    self._out.print()
                    self._mid_line = False

    def _line(self, text: str, wrap: bool = False) -> None:
        """Progress lines stay on one line, cut to the terminal width; errors wrap in full."""
        if self._mid_line:
            self._out.print()
            self._mid_line = False
        self._out.print(
            text, highlight=False, no_wrap=not wrap, overflow="fold" if wrap else "ellipsis"
        )

    def _sql(self, sql: str) -> str:
        flat = " ".join(sql.split())
        text = flat if len(flat) <= self.SQL_CHARS else flat[: self.SQL_CHARS - 1] + "…"
        return escape(text)


@contextmanager
def _database_errors(failed: str) -> Iterator[None]:
    try:
        yield
    except ConnectionRefused as e:
        console.print(f"[bold red]Refused:[/] {e}")
        raise typer.Exit(1) from e
    except QueryRejected as e:
        console.print(f"[bold red]{failed}:[/] a query was rejected: {e.reason}")
        raise typer.Exit(1) from e
    except psycopg.OperationalError as e:
        console.print(f"[bold red]Cannot connect:[/] {str(e).strip()}")
        raise typer.Exit(1) from e
    except psycopg.Error as e:
        console.print(f"[bold red]{failed}:[/] {str(e).strip()}")
        raise typer.Exit(1) from e


def _render_run(service: AnalyzerService, run: Run, top: int = 10) -> None:
    measured = sorted(service.storage(run.id), key=lambda s: -s.total_bytes)
    table = Table(title=f"Largest of {len(measured)} tables (Run {run.id[:8]}, {run.status})")
    for column in ("#", "Table", "Total", "Heap", "Indexes", "TOAST", "Rows (estimate)"):
        table.add_column(column, justify="left" if column == "Table" else "right")
    for rank, s in enumerate(measured[:top], start=1):
        table.add_row(
            str(rank),
            s.ref.qualified,
            format_bytes(s.total_bytes),
            format_bytes(s.data_bytes),
            format_bytes(s.index_bytes),
            format_bytes(s.toast_bytes) if s.toast_bytes is not None else "-",
            "unknown" if s.row_count is None else f"{s.row_count:,}",
        )
    console.print(table)


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
