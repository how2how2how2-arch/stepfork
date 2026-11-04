"""`stepfork coverage` command."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from stepfork.inspect.coverage import (
    CaptureState,
    CoverageReport,
    coverage_bundle,
)
from stepfork.trace import TraceStorageError

console = Console()


def coverage_command(
    path: Annotated[
        Path,
        typer.Argument(
            help="Path to a .sftrace directory bundle.",
            file_okay=False,
            dir_okay=True,
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the machine-readable JSON report."),
    ] = False,
    calls: Annotated[
        bool,
        typer.Option("--calls", help="List every observed call and its state."),
    ] = False,
) -> None:
    """Report instrumentation coverage for a `.sftrace` bundle.

    The report covers the dependency calls the bundle recorded. It cannot count
    calls StepFork never instrumented, and reports that limit instead of a zero.
    """
    try:
        report = coverage_bundle(path)
    except (TraceStorageError, OSError, ValueError) as exc:
        console.print(f"[red]Error reading bundle:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_output:
        # sys.stdout rather than the rich console: rich wraps long lines, and a
        # wrapped JSON document is not JSON for whoever reads it next.
        sys.stdout.write(report.model_dump_json())
        sys.stdout.write("\n")
        return

    _print_report(report, show_calls=calls)


def _print_report(report: CoverageReport, *, show_calls: bool) -> None:
    console.print()
    console.print("[bold]Stepfork Coverage[/bold]")
    console.print()
    console.print(f"Bundle:  {report.path}")
    console.print(f"Agent:   {report.agent_name}")
    console.print()

    table = Table.grid(padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column()
    table.add_row(
        "Observed calls",
        f"{report.observed_calls} (tool {report.tool_calls}, llm {report.llm_calls})",
    )
    for state, count in (
        (CaptureState.CAPTURED, report.captured_calls),
        (CaptureState.UNFINGERPRINTED, report.unfingerprinted_calls),
        (CaptureState.MISSING, report.missing_calls),
    ):
        table.add_row(f"  {state.value}", str(count))
    table.add_row(
        "Input fingerprints",
        f"{report.input_fingerprinted_calls}/{report.observed_calls}",
    )
    console.print(table)
    console.print()

    rate = report.capture_rate()
    if rate is None:
        console.print(
            "Capture rate: [yellow]NOT MEASURED[/yellow] "
            "(the bundle records no dependency calls)"
        )
    else:
        console.print(
            f"Capture rate: {report.captured_calls}/{report.observed_calls} "
            f"observed calls ({rate:.1%})"
        )
    console.print(
        "Uninstrumented calls: [yellow]not measurable[/yellow] "
        f"({report.uninstrumented_calls!r})"
    )
    console.print(f"  {report.uninstrumented_reason}")
    console.print()

    if show_calls:
        _print_calls(report)


def _print_calls(report: CoverageReport) -> None:
    table = Table(title="Observed calls")
    table.add_column("#", justify="right")
    table.add_column("Kind")
    table.add_column("Label")
    table.add_column("State")
    table.add_column("Input digest")
    for call in report.calls:
        table.add_row(
            str(call.index),
            call.kind,
            call.label,
            call.state.value,
            "yes" if call.input_fingerprinted else "no",
        )
    console.print(table)
    console.print()
