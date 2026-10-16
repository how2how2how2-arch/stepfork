"""`stepfork export` command."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from stepfork.assertions import validate_trajectory_expectation
from stepfork.export.entrypoint import EntrypointError, resolve_entrypoint
from stepfork.export.generator import (
    ExportError,
    ExportExistsError,
    export_pytest_test,
)
from stepfork.export.mutants import recorded_output
from stepfork.trace import (
    JsonValue,
    Trace,
    TraceStorageError,
    validate_bundle,
)
from stepfork.trace.redaction import redact_json
from stepfork.trace.replay_policy import ReplayPolicy

console = Console()
RECORDED_OUTPUT_WARNING = (
    "[yellow]warning:[/yellow] no --expect-output given; the test will "
    "assert the recorded run output. That passes against the code that "
    "produced the recording. Pass --expect-output to assert the corrected "
    "behavior instead."
)


def export_command(
    path: Annotated[
        Path,
        typer.Argument(
            help="Path to a .sftrace directory bundle.",
            exists=False,
            file_okay=False,
            dir_okay=True,
        ),
    ],
    pytest_format: Annotated[
        bool,
        typer.Option(
            "--pytest/--no-pytest",
            help="Export a pytest test (the only v0.1 format).",
        ),
    ] = True,
    entrypoint: Annotated[
        str | None,
        typer.Option(
            "--entrypoint",
            help="Trusted local MODULE:FUNCTION the generated test executes.",
        ),
    ] = None,
    expect_output: Annotated[
        Path | None,
        typer.Option(
            "--expect-output",
            help="JSON file defining the expected regression outcome.",
        ),
    ] = None,
    expect_trajectory: Annotated[
        Path | None,
        typer.Option(
            "--expect-trajectory",
            help="Reviewed JSON file with tool and step expectations.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Destination test file (default: ./test_<trace>_regression.py).",
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Replace the output file if it already exists.",
        ),
    ] = False,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="Replay mode used by the generated test (frozen recommended).",
        ),
    ] = "frozen",
    allow_live_tool: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-live-tool",
            help="Authorize one named tool in exported live mode; repeat per tool.",
        ),
    ] = None,
) -> None:
    """Export an executable pytest regression test from a trace.

    The generated test loads the frozen trace, executes your trusted
    entrypoint under replay, and fails when behavior diverges from the
    expected outcome.
    """
    if not pytest_format:
        console.print("[red]v0.1 supports pytest export only (--pytest).[/red]")
        raise typer.Exit(2)

    try:
        ReplayPolicy(mode)
    except ValueError:
        console.print(f"[red]Invalid replay mode {mode!r}.[/red]")
        raise typer.Exit(2) from None
    if allow_live_tool and mode != "live":
        console.print("[red]--allow-live-tool requires --mode live.[/red]")
        raise typer.Exit(2)

    if entrypoint is None:
        console.print(
            "[red]An executable regression test needs --entrypoint "
            "MODULE:FUNCTION.[/red]"
        )
        raise typer.Exit(2)

    result = validate_bundle(path, strict=True)
    if not result.valid:
        console.print("[red]Trace failed validation.[/red]")
        for issue in result.issues:
            console.print(f"  {issue.code}: {issue.message}")
        raise typer.Exit(2)

    try:
        trace = Trace.load(path)
    except TraceStorageError as exc:
        console.print("[red]Unable to read trace.[/red]")
        console.print(_sanitize_text(str(exc)))
        raise typer.Exit(2) from exc

    try:
        resolve_entrypoint(entrypoint, import_root=Path.cwd())
    except EntrypointError as exc:
        console.print("[red]Unable to resolve entrypoint.[/red]")
        console.print(_sanitize_text(str(exc)))
        raise typer.Exit(2) from exc

    warning: str | None = None
    if expect_output is not None:
        expectation = _load_expectation(expect_output)
        has_expectation = True
    else:
        expectation = recorded_output(trace)
        has_expectation = expectation is not None
        warning = RECORDED_OUTPUT_WARNING

    trajectory_expectation = (
        _load_expectation(expect_trajectory) if expect_trajectory is not None else None
    )
    if trajectory_expectation is not None:
        try:
            validate_trajectory_expectation(trajectory_expectation)
        except ValueError as exc:
            console.print(f"[red]Invalid --expect-trajectory:[/red] {exc}")
            raise typer.Exit(2) from exc

    destination = output or Path(f"test_{_safe_stem(path)}_regression.py")
    if destination.exists() and not overwrite:
        console.print(
            f"[red]{destination} already exists; pass --overwrite to replace it.[/red]"
        )
        raise typer.Exit(1)

    try:
        written = export_pytest_test(
            trace_path=path,
            output_path=destination,
            entrypoint=entrypoint,
            expectation=expectation,
            has_expectation=has_expectation,
            trajectory_expectation=trajectory_expectation,
            mode=mode,
            allow_live_tools=set(allow_live_tool) if allow_live_tool else None,
            import_root=Path.cwd(),
            overwrite=overwrite,
        )
    except ExportExistsError as exc:
        console.print(f"[red]{_sanitize_text(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    except ExportError as exc:
        console.print(f"[red]Export failed: {_sanitize_text(str(exc))}[/red]")
        raise typer.Exit(1) from exc

    if warning:
        console.print(warning)
    console.print(f"[green]Wrote[/green] {written}")
    console.print(f"Entrypoint: {entrypoint}")
    console.print(f"Mode:       {mode}")
    if has_expectation:
        console.print(f"Expectation: {_sanitize_text(json.dumps(expectation))}")
    else:
        console.print("Expectation: none (replay fidelity only)")
    if trajectory_expectation is not None:
        console.print("Trajectory: reviewed JSON expectation")
    raise typer.Exit(0)


def _load_expectation(path: Path) -> JsonValue:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        console.print(f"[red]Cannot read --expect-output:[/red] {exc}")
        raise typer.Exit(2) from exc
    except json.JSONDecodeError as exc:
        console.print(f"[red]--expect-output is not valid JSON:[/red] {exc.msg}")
        raise typer.Exit(2) from exc
    return redact_json(payload).value


def _safe_stem(path: Path) -> str:
    stem = re.sub(r"\W+", "_", path.stem).strip("_")
    return stem or "trace"


def _sanitize_text(value: str) -> str:
    sanitized = redact_json(value).value
    if isinstance(sanitized, str):
        return sanitized
    return str(sanitized)
