"""`stepfork run` command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from stepfork.export.receipt import (
    EXIT_BY_VERDICT,
    VERDICT_COULD_NOT_EXECUTE,
    VERDICT_FAILED,
    VERDICT_PASSED,
    Receipt,
    run_exported_test,
    write_receipt,
)

console = Console()

STYLE = {
    VERDICT_PASSED: "[green]PASSED[/green]",
    VERDICT_FAILED: "[red]FAILED[/red]",
    VERDICT_COULD_NOT_EXECUTE: "[yellow]COULD NOT EXECUTE[/yellow]",
}


def run_command(
    test: Annotated[
        Path,
        typer.Argument(
            help="Path to the pytest file `stepfork export` wrote.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    bundle: Annotated[
        Path | None,
        typer.Option(
            "--bundle",
            help="Name the .sftrace bundle instead of reading it from the test.",
        ),
    ] = None,
    receipt: Annotated[
        Path | None,
        typer.Option(
            "--receipt",
            help="Write a JSON receipt of what the runner observed.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the receipt to stdout as JSON."),
    ] = False,
    pytest_arg: Annotated[
        list[str] | None,
        typer.Option(
            "--pytest-arg",
            help="Extra argument for the pytest subprocess; repeat per argument.",
        ),
    ] = None,
    timeout_seconds: Annotated[
        float | None,
        typer.Option(
            "--timeout-seconds",
            help="Stop the check after this long and report could_not_execute.",
        ),
    ] = None,
) -> None:
    """Run an exported regression test and write the receipt yourself.

    `stepfork export` writes a test; this command runs it. The difference from
    running pytest directly is the verdict: a check that could not execute --
    the bundle is missing, a fixture cannot be built, nothing was collected --
    is reported as `could_not_execute`, which is neither a pass nor a caught
    regression. pytest's exit status does not carry that distinction: measured
    on pytest 9.1.1 a missing bundle and a changed return value both exit 1 and
    both land in JUnit as `failure`.

    The receipt is written by this process after the child exits, never by the
    test, and it names the bundle digest and the test hash the verdict rests
    on.
    """
    found: Receipt = run_exported_test(
        test,
        bundle=bundle,
        pytest_args=pytest_arg,
        timeout_seconds=timeout_seconds,
    )

    if json_output:
        console.print_json(json.dumps(found.to_json(), sort_keys=True))
    else:
        _report(found)

    if receipt is not None:
        write_receipt(found, receipt)
        if not json_output:
            console.print(f"Receipt: {receipt}")

    raise typer.Exit(EXIT_BY_VERDICT[found.verdict])


def _report(found: Receipt) -> None:
    table = Table.grid(padding=(0, 4))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Test", found.test)
    table.add_row("Test sha256", found.test_sha256[:16] or "(unreadable)")
    table.add_row("Bundle", found.bundle or "(none)")
    table.add_row(
        "Bundle digest",
        (found.bundle_digest or "(none)")[:16],
    )
    if found.exit_status is not None:
        table.add_row("pytest exit", str(found.exit_status))
    counts = found.counts
    table.add_row(
        "Collected",
        f"{counts.tests} test(s), {counts.failures} failure(s), "
        f"{counts.errors} error(s), {counts.skipped} skipped",
    )
    console.print(table)
    console.print()
    console.print(f"Verdict: {STYLE[found.verdict]}")
    if found.reason is not None:
        console.print(f"Reason: {found.reason}")
    if found.detail:
        console.print(f"{found.detail}")
    if found.verdict == VERDICT_COULD_NOT_EXECUTE:
        console.print(
            "The check did not execute, so this run says nothing about the "
            "agent's behaviour."
        )
    console.print()
