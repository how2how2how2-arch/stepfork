"""CLI command for forking traces."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from stepfork.fork import ForkPoint, ForkResult, fork_trace
from stepfork.trace.models import Trace

console = Console()


def fork_command(
    path: Annotated[
        Path,
        typer.Argument(
            help="Path to the `.sftrace` bundle to fork",
            exists=True,
            dir_okay=True,
            file_okay=False,
        ),
    ],
    at_step: Annotated[
        int | None,
        typer.Option(
            "--at-step",
            help="Fork at this step number (0-indexed)",
        ),
    ] = None,
    at_event: Annotated[
        str | None,
        typer.Option(
            "--at-event",
            help="Fork at this event ID",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Path for the forked `.sftrace` bundle",
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Overwrite the output bundle if it exists",
        ),
    ] = False,
) -> None:
    """Fork a trace at a specific step.

    Creates a new trace containing all events up to and including the
    specified fork point. The forked trace preserves parent-child
    relationships and structural integrity.

    Example:

        stepfork fork failure.sftrace --at-step 2 --output forked.sftrace
    """
    console.print()
    console.print("[bold]Stepfork Fork[/bold]")
    console.print()

    # Validate fork point specification
    if at_step is None and at_event is None:
        console.print("[red]Error: must specify either --at-step or --at-event[/red]")
        raise typer.Exit(2)

    if at_step is not None and at_event is not None:
        console.print("[red]Error: cannot specify both --at-step and --at-event[/red]")
        raise typer.Exit(2)

    # Load the original trace
    try:
        trace = Trace.load(path)
    except Exception as exc:
        console.print(f"[red]Error loading trace:[/red] {exc}")
        raise typer.Exit(2) from exc

    # Display trace info
    console.print(f"Original trace:  {path}")
    console.print(f"Agent:           {trace.agent_name}")
    console.print(f"Events:          {len(trace.events)}")

    if at_step is not None:
        console.print(f"Fork at step:    {at_step}")
    if at_event is not None:
        console.print(f"Fork at event:   {at_event}")

    console.print()

    # Determine output path
    if output is None:
        output = path.parent / f"{path.stem}-forked.sftrace"

    # Create fork point
    fork_point = ForkPoint(event_id=at_event, step=at_step)

    # Execute fork
    console.print("[yellow]Forking trace...[/yellow]")

    result = fork_trace(
        path,
        fork_point,
        output_path=output,
        overwrite=overwrite,
    )

    # Check if fork succeeded
    if not result.success:
        console.print()
        console.print(f"[red]✗ Fork failed:[/red] {result.error_message}")
        raise typer.Exit(1)

    # Display report
    _display_report(result)

    console.print()
    console.print(f"[green]✓ Forked trace saved to:[/green] {output}")
    console.print()


def _display_report(result: ForkResult) -> None:
    """Display a formatted fork report."""
    console.print()

    # Create summary table
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column()

    table.add_row("Original Events", str(result.original_event_count))
    table.add_row("Forked Events", str(result.forked_event_count))
    table.add_row("Fork Point Step", str(result.fork_point_step))
    table.add_row("Fork Point Event", result.fork_point_event_id[:16] + "...")

    excluded = result.original_event_count - result.forked_event_count
    table.add_row("Events Excluded", str(excluded))

    if result.overrides_applied > 0:
        table.add_row("Overrides Applied", str(result.overrides_applied))

    if result.overridden_steps:
        steps = ", ".join(str(step) for step in result.overridden_steps)
        table.add_row("Overridden Steps", steps)

    if result.stale_event_ids:
        table.add_row(
            "Potentially Stale Outputs",
            f"[yellow]{len(result.stale_event_ids)}[/yellow]",
        )

    console.print(Panel(table, title="[bold]Fork Report[/bold]", expand=False))

    if result.stale_event_ids:
        console.print(
            "[yellow]note[/yellow]: these outputs were recorded under the "
            "upstream state an override changed. Replaying them is not "
            "evidence for the modified path."
        )
