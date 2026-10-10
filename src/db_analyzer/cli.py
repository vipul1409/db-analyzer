"""`dbx` command line. A thin layer over AnalyzerService."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

import psycopg
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from db_analyzer.agent.events import AgentEvent
from db_analyzer.agent.llm import LLMConfigError
from db_analyzer.analyzers.workload import MIN_STATS_WINDOW
from db_analyzer.core.model import (
    Connection,
    ConnectionRefused,
    FindingStatus,
    ProbeResult,
    QueryRejected,
    Run,
    SettableStatus,
    StorageStats,
    UnknownCollections,
    WorkloadReport,
)
from db_analyzer.core.run_view import RunView
from db_analyzer.core.units import format_bytes
from db_analyzer.runs import DEFAULT as DEFAULT_ANALYZERS
from db_analyzer.runs import OptionsNotAccepted, RunOptions, UnknownAnalyzer, chosen_analyzers
from db_analyzer.service import CURRENT_STATUSES, AnalyzerService

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

MIN_STATS_WINDOW_HOURS = MIN_STATS_WINDOW.total_seconds() / 3600
_OPTION_FLAGS = {
    "collections": "--table",
    "exact_counts": "--exact-counts",
    "min_stats_window": "--min-stats-window",
}
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
    alias_identifiers: Annotated[
        bool | None,
        typer.Option(
            "--alias-identifiers/--no-alias-identifiers",
            help="Show schema, table and column names to the LLM only as aliases. "
            "Omitted: keep the current setting (off for a new Connection).",
        ),
    ] = None,
) -> None:
    """Add (or update) a Connection to one database and show what the analyzer can see."""
    service = AnalyzerService()
    connection = service.add_connection(
        name=name, dsn_env=dsn_env, alias_identifiers=alias_identifiers
    )
    with _database_errors("Probe failed"):
        probe = service.probe(connection.id)
    _render_probe(name, probe)
    if connection.alias_identifiers:
        console.print("[dim]The LLM sees schema, table and column names only as aliases.[/]")


@app.command()
def analyze(
    name: Annotated[str, typer.Argument(help="Connection to analyse (see `dbx connect`).")],
    report: Annotated[
        Path | None,
        typer.Option(
            help="Write a report of the Run to this file: JSON if it ends in .json, else Markdown."
        ),
    ] = None,
    table: Annotated[
        list[str] | None,
        typer.Option(help="Analyse only this table (schema.table); repeat for more."),
    ] = None,
    exact_counts: Annotated[
        bool,
        typer.Option(
            help="Also count rows with count(*), for each table the EXPLAIN gate allows; "
            "the others keep their estimate and show why."
        ),
    ] = False,
    analyzer: Annotated[
        list[str] | None,
        typer.Option(
            "--analyzer",
            "-a",
            help="What to analyse: inventory (sizes, index health) or workload (the most "
            "expensive statements, from pg_stat_statements, else Azure Query Store). Repeat for "
            "both. Default: inventory.",
        ),
    ] = None,
    min_stats_window: Annotated[
        float,
        typer.Option(
            help="Hours the statement statistics must cover before the workload is ranked; "
            "younger statistics are refused."
        ),
    ] = MIN_STATS_WINDOW_HOURS,
) -> None:
    """Run the deterministic analysis (no LLM): tables by size and ranked Findings; with
    `-a workload`, the most expensive statements."""
    service = AnalyzerService()
    connection = _connection(service, name)
    chosen = analyzer or list(DEFAULT_ANALYZERS)
    options = RunOptions(table or None, exact_counts, timedelta(hours=min_stats_window))
    try:
        chosen_analyzers(chosen, options)
    except UnknownAnalyzer as e:
        console.print(f"[bold red]{escape(str(e))}[/]")
        raise typer.Exit(1) from e
    except OptionsNotAccepted as e:
        flags = " and ".join(_OPTION_FLAGS[o] for o in e.options)
        verb = "applies" if len(e.options) == 1 else "apply"
        console.print(f"[bold red]{flags} {verb} to the {e.analyzer} analyzer.[/]")
        raise typer.Exit(1) from e
    with _database_errors("Analysis failed"):
        try:
            run = service.run(
                connection.id,
                chosen,
                collections=options.collections,
                exact_counts=options.exact_counts,
                min_stats_window=options.min_stats_window,
            )
        except UnknownCollections as e:
            console.print(f"[bold red]{e}[/]")
            raise typer.Exit(1) from e
    _render_run(service.run_view(run.id))
    if report is not None:
        fmt: Literal["md", "json"] = "json" if report.suffix.lower() == ".json" else "md"
        report.write_bytes(service.export(run.id, fmt))
        console.print(f"Report written to [bold]{report}[/]")


@app.command()
def findings(
    name: Annotated[str, typer.Argument(help="Connection (see `dbx connect`).")],
    all_: Annotated[
        bool, typer.Option("--all", help="Include obsolete Findings (subject no longer exists).")
    ] = False,
) -> None:
    """List the Connection's Findings with their status. "fixed?" marks a Finding the latest
    Run covering its subject did not observe: confirm with `dbx fixed`."""
    service = AnalyzerService()
    connection = _connection(service, name)
    statuses: tuple[FindingStatus, ...] = (
        (*CURRENT_STATUSES, "obsolete") if all_ else CURRENT_STATUSES
    )
    listed = service.findings(connection.id, statuses)
    if not listed:
        console.print("No Findings yet. Run `dbx analyze` first.")
        return
    table = Table(title=f"Findings on '{name}'")
    for column in ("Fingerprint", "Status", "Last seen", ""):
        table.add_column(column)
    for f in listed:
        table.add_row(
            escape(f.fingerprint),
            f.status,
            f"Run {f.last_seen_run[:8]}",
            "[yellow]fixed?[/]" if f.unobserved_by else "",
        )
    console.print(table)


@app.command()
def ack(
    name: Annotated[str, typer.Argument(help="Connection.")],
    fingerprint: Annotated[str, typer.Argument(help="Finding to acknowledge.")],
) -> None:
    """Acknowledge a Finding: it stays acknowledged when later Runs observe it again."""
    _set_status(name, fingerprint, "acknowledged")


@app.command()
def fixed(
    name: Annotated[str, typer.Argument(help="Connection.")],
    fingerprint: Annotated[str, typer.Argument(help="Finding to mark fixed.")],
) -> None:
    """Confirm a Finding fixed. A later Run that observes it again reopens it."""
    _set_status(name, fingerprint, "fixed")


@app.command()
def reopen(
    name: Annotated[str, typer.Argument(help="Connection.")],
    fingerprint: Annotated[str, typer.Argument(help="Finding to reopen.")],
) -> None:
    """Set a Finding back to open, e.g. to answer "fixed?" with no."""
    _set_status(name, fingerprint, "open")


@app.command()
def runs(name: Annotated[str, typer.Argument(help="Connection.")]) -> None:
    """List the Connection's Runs, oldest first."""
    service = AnalyzerService()
    connection = _connection(service, name)
    table = Table(title=f"Runs on '{name}'")
    for column in ("Run", "Started", "Status", "Measured", "Skipped"):
        table.add_column(column)
    for r in service.runs(connection.id):
        table.add_row(
            r.id[:8],
            r.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            r.status,
            str(sum(len(refs) for refs in r.scope.values())),
            str(sum(len(items) for items in r.skipped.values())),
        )
    console.print(table)


@app.command()
def compare(
    name: Annotated[str, typer.Argument(help="Connection.")],
    run_a: Annotated[str, typer.Argument(help="A Run id, or its first characters.")],
    run_b: Annotated[str, typer.Argument(help="Another Run id, or its first characters.")],
) -> None:
    """Compare two Runs over the collections both measured: size changes and Findings that
    appeared or disappeared. Collections only one Run measured are listed, not compared."""
    service = AnalyzerService()
    connection = _connection(service, name)
    known = [r.id for r in service.runs(connection.id)]
    c = service.compare_runs(_run_id(known, run_a), _run_id(known, run_b))
    shared = sum(len(names) for names in c.shared.values())
    console.print(f"Run {c.before[:8]} → Run {c.after[:8]}, over {shared} shared collections")
    table = Table(title="Size changes")
    for column in ("Table", "Before", "After", "Change"):
        table.add_column(column, justify="left" if column == "Table" else "right")
    for change in c.size_changes:
        sign = "+" if change.delta_bytes >= 0 else "-"
        table.add_row(
            change.collection,
            format_bytes(change.before_bytes),
            format_bytes(change.after_bytes),
            sign + format_bytes(abs(change.delta_bytes)),
        )
    console.print(table)
    for fingerprint in c.appeared:
        console.print(f"[red]appeared:[/] {escape(fingerprint)}")
    for fingerprint in c.disappeared:
        console.print(f"[green]disappeared:[/] {escape(fingerprint)}")
    for analyzer, names in c.not_compared.items():
        console.print(f"[dim]{analyzer}, not compared (one Run only): {', '.join(names)}[/]")


def _connection(service: AnalyzerService, name: str) -> Connection:
    try:
        return service.connection(name)
    except KeyError as e:
        console.print(f"[bold red]No Connection named {name!r}.[/] Add it with `dbx connect`.")
        raise typer.Exit(1) from e


def _set_status(name: str, fingerprint: str, status: SettableStatus) -> None:
    service = AnalyzerService()
    connection = _connection(service, name)
    try:
        f = service.set_finding_status(connection.id, fingerprint, status)
    except KeyError as e:
        console.print(f"[bold red]No Finding {escape(fingerprint)!r} on {name!r}.[/]")
        raise typer.Exit(1) from e
    console.print(f"{escape(f.fingerprint)}: {f.status}")


def _run_id(known: list[str], prefix: str) -> str:
    matches = [r for r in known if r.startswith(prefix)]
    if len(matches) != 1:
        problem = "No Run" if not matches else "More than one Run"
        console.print(f"[bold red]{problem} starts with {prefix!r}.[/] See `dbx runs`.")
        raise typer.Exit(1)
    return matches[0]


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


def _render_run(view: RunView, top: int = 10) -> None:
    if view.storage is not None:
        _render_sizes(view.run, view.storage, top)
    else:
        console.print(f"Run {view.run.id[:8]}, {view.run.status}")
    if view.workload is not None:
        _render_workload(view.workload)
    for rank, o in enumerate(view.problems, start=1):
        console.print(f"{rank}. [bold]{o.severity}[/] {escape(o.fingerprint)}: {escape(o.title)}")
    for s in view.skipped:
        what = "collection" if s.measurement is None else s.measurement.replace("_", " ")
        console.print(f"[yellow]{s.collection}: {what} skipped:[/] {escape(s.reason)}")


def _render_workload(w: WorkloadReport) -> None:
    for warning in w.warnings:
        console.print(f"[yellow]Warning:[/] {escape(warning)}")
    if w.source is None:
        console.print("[bold]To rank slow statements, enable pg_stat_statements:[/]")
        for n, step in enumerate(w.enable_steps, start=1):
            console.print(f"  {n}. {escape(step)}")
        console.print("[dim]Reviewing the schema instead.[/]")
        return
    for why, n in sorted(w.excluded.items()):
        console.print(f"[dim]Left out of the ranking: {n} with {why}[/]")
    if not w.items:
        return
    table = Table(title=f"Most expensive of {w.statements:,} statements ({w.source})")
    for column in ("#", "Fingerprint", "Calls", "Total", "Mean", "Share", "Statement"):
        table.add_column(
            column, justify="left" if column in ("Fingerprint", "Statement") else "right"
        )
    for rank, i in enumerate(w.items[:10], start=1):
        table.add_row(
            str(rank),
            i.fingerprint,
            f"{i.calls:,}",
            f"{i.total_ms / 1000:,.1f} s",
            f"{i.mean_ms:,.1f} ms",
            f"{i.share_of_time:.0%}",
            escape(i.text if len(i.text) <= 70 else i.text[:69] + "…"),
        )
    console.print(table)


def _render_sizes(run: Run, measured: list[StorageStats], top: int) -> None:
    table = Table(title=f"Largest of {len(measured)} tables (Run {run.id[:8]}, {run.status})")
    for column in ("#", "Table", "Total", "Heap", "Indexes", "TOAST", "Rows"):
        table.add_column(column, justify="left" if column == "Table" else "right")
    for rank, s in enumerate(measured[:top], start=1):
        table.add_row(
            str(rank),
            s.ref.qualified,
            format_bytes(s.total_bytes),
            format_bytes(s.data_bytes),
            format_bytes(s.index_bytes),
            format_bytes(s.toast_bytes) if s.toast_bytes is not None else "-",
            "unknown" if s.row_count is None else f"{s.row_count:,} ({s.row_count_method})",
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
    if p.host_type == "azure_flexible":
        capture = p.settings.get("pg_qs.query_capture_mode") or "none"
        connect = (
            "CONNECT on azure_sys" if p.privileges.azure_sys_connect else "no CONNECT on azure_sys"
        )
        table.add_row("Query Store", f"capture {capture}, {connect}")
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
