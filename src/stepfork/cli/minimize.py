"""CLI command for trace minimization."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from stepfork.minimize.delta_debug import create_minimized_trace
from stepfork.minimize.engine import MinimizationEngine, generate_report
from stepfork.minimize.models import MinimizationReport
from stepfork.minimize.signature import ReplayNeverReachedRecordedCall
from stepfork.trace.models import Trace

console = Console()


def minimize_command(
    path: Annotated[
        Path,
        typer.Argument(
            help="Path to the `.sftrace` bundle to minimize",
            exists=True,
            dir_okay=True,
            file_okay=False,
        ),
    ],
    entrypoint: Annotated[
        str,
        typer.Option(
            "--entrypoint",
            "-e",
            help="Agent entrypoint as MODULE:FUNCTION",
        ),
    ],
    expected_output: Annotated[
        Path | None,
        typer.Option(
            "--expect-output",
            help="JSON file with expected output for comparison",
            exists=True,
        ),
    ] = None,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            "-m",
            help="Replay mode: frozen (default), live, forbidden, manual, or derived",
        ),
    ] = "frozen",
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Path for the minimized `.sftrace` bundle",
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Overwrite the output bundle if it exists",
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            help="Timeout in seconds for each replay attempt",
        ),
    ] = 30.0,
    attempts: Annotated[
        int,
        typer.Option(
            "--attempts",
            help="Number of attempts per subset test (for nondeterminism detection)",
        ),
    ] = 1,
) -> None:
    """Minimize a failing trace to the smallest reproducible subset.

    Finds the minimal set of events that preserves the original failure
    using dependency-aware delta debugging.

    Example:

        stepfork minimize failure.sftrace --entrypoint agent:run_agent \\
            --output minimal.sftrace
    """
    console.print()
    console.print("[bold]Stepfork Minimize[/bold]")
    console.print()

    # Load the original trace
    try:
        trace = Trace.load(path)
    except Exception as exc:
        console.print(
            f"[red]Error loading trace:[/red] {exc}",
        )
        raise typer.Exit(2) from exc

    # Display trace info
    console.print(f"Original trace:  {path}")
    console.print(f"Agent:           {trace.agent_name}")
    console.print(f"Events:          {len(trace.events)}")
    console.print(f"Entrypoint:      {entrypoint}")
    console.print(f"Mode:            {mode}")
    console.print()

    # Load expected output if provided
    expected_output_value = None
    if expected_output is not None:
        try:
            import json

            expected_output_value = json.loads(expected_output.read_text())
        except Exception as exc:
            console.print(f"[red]Error loading expected output:[/red] {exc}")
            raise typer.Exit(2) from exc

    # Run minimization
    console.print("[yellow]Detecting failure signature...[/yellow]")
    try:
        from stepfork.trace.replay_policy import ReplayPolicy

        ReplayPolicy(mode)
    except ValueError:
        console.print(f"[red]Invalid replay mode {mode!r}.[/red]")
        raise typer.Exit(2) from None
    engine = MinimizationEngine(
        trace,
        entrypoint,
        expected_output=expected_output_value,
        mode=mode,
        timeout_seconds=timeout,
        max_attempts_per_test=attempts,
    )

    try:
        with console.status("[yellow]Minimizing trace...[/yellow]") as status:
            result = engine.minimize()
            status.stop()
    except KeyboardInterrupt:
        console.print("\n[yellow]Minimization interrupted by user[/yellow]")
        raise typer.Exit(1) from None
    except ReplayNeverReachedRecordedCall as exc:
        # The run could not reach the failure it was asked to minimize, so it
        # says nothing about the trace. Exit 2 separates that from a failed
        # minimization (exit 1) and from invalid input, which prints its own
        # message. See issue #22.
        console.print(f"\n[yellow]✗ Cannot minimize:[/yellow] {exc}")
        raise typer.Exit(2) from None
    except Exception as exc:
        console.print(
            f"\n[red]Minimization failed:[/red] {exc}",
        )
        raise typer.Exit(1) from exc

    # Check if minimization succeeded
    if not result.success:
        console.print()
        console.print(f"[red]✗ Minimization failed:[/red] {result.error_message}")
        raise typer.Exit(1)

    # Determine output path
    if output is None:
        output = path.parent / f"{path.stem}-minimized.sftrace"

    # Save the minimized trace
    try:
        from stepfork.minimize.delta_debug import EventSubset

        minimal_subset = EventSubset(
            events=tuple(
                trace.events[i] for i in range(result.minimized_event_count or 0)
            ),
            includes_dependency_calls=True,
        )
        minimized_trace = create_minimized_trace(trace, minimal_subset)
        minimized_trace.save(output, overwrite=overwrite)
    except Exception as exc:
        console.print(f"\n[red]Error saving minimized trace:[/red] {exc}")
        raise typer.Exit(1) from exc

    # Generate and display report
    report = generate_report(result, str(path), str(output))
    _display_report(report)

    console.print()
    console.print(f"[green]✓ Minimized trace saved to:[/green] {output}")
    console.print()


def _display_report(report: MinimizationReport) -> None:
    """Display a formatted minimization report."""
    console.print()

    # Create summary table
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column()

    table.add_row("Original Events", str(report.original_event_count))
    if report.minimized_event_count is not None:
        table.add_row("Minimized Events", str(report.minimized_event_count))
    if report.reduction_percentage is not None:
        table.add_row("Reduction", f"{report.reduction_percentage:.1f}%")
    table.add_row("Replay Attempts", str(report.replay_attempts))
    table.add_row("Failure Signature", report.failure_signature)
    table.add_row(
        "Minimal",
        "[green]Yes[/green]" if report.is_minimal else "[yellow]No[/yellow]",
    )

    if report.nondeterministic:
        table.add_row(
            "Nondeterministic",
            "[yellow]Yes[/yellow]",
        )

    console.print(Panel(table, title="[bold]Minimization Report[/bold]", expand=False))

    # Display warnings if any
    if report.warnings:
        console.print()
        console.print("[yellow]Warnings:[/yellow]")
        for warning in report.warnings:
            console.print(f"  • {warning}")
