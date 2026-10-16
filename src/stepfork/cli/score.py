"""`stepfork score` command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from stepfork.export.mutants import (
    PinCoverage,
    derive_battery,
    pin_coverage,
    recorded_output,
)
from stepfork.export.scoring import ExpectationScore, score_expectation
from stepfork.export.testfile import ExportedTestError, read_exported_test
from stepfork.trace import JsonValue, Trace, TraceStorageError, redact_json

console = Console()

EXIT_ANCHORED = 0
EXIT_RUN_PINNED = 1
EXIT_CANNOT_SCORE = 2


def score_command(
    test: Annotated[
        Path,
        typer.Argument(
            help="Exported pytest regression test (read, never executed).",
            exists=False,
            file_okay=True,
            dir_okay=False,
        ),
    ],
    expect_output: Annotated[
        Path | None,
        typer.Option(
            "--expect-output",
            help="Score this expectation instead of the one the test embeds.",
        ),
    ] = None,
    bundle: Annotated[
        Path | None,
        typer.Option(
            "--bundle",
            help="Trace bundle to read the recorded run from "
            "(default: the TRACE_PATH the test names).",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the score as JSON."),
    ] = False,
) -> None:
    """Score an exported expectation against a battery derived from the run.

    The battery is derived, not executed: the claim-drawn row is the recorded
    run's own output, and the code-drawn rows are perturbations of the
    expectation's leaves. A claim-pinned expectation rejects the recorded run;
    a run-pinned one accepts it, which means the generated test passes against
    the code that produced the recording.

    Exits 0 when the expectation anchors the claim, 1 when it is run-pinned,
    and 2 when the question cannot be answered (no expectation, no recorded
    run to draw a claim-drawn mutant from, or a file that cannot be read).
    """
    try:
        exported = read_exported_test(test)
    except ExportedTestError as exc:
        _fail(str(exc))
        return

    if expect_output is not None:
        expectation = _load_json(expect_output)
        if expectation is None:
            return
        has_expectation = True
    else:
        expectation = exported.expectation
        has_expectation = exported.has_expectation

    destination = bundle if bundle is not None else exported.trace_path
    if destination is None:
        _fail(
            f"{test} names no trace bundle; pass --bundle to name the .sftrace "
            "bundle to read the recorded run from"
        )
        return
    if not destination.exists():
        _fail(f"trace bundle not found: {destination}")
        return

    try:
        trace = Trace.load(destination)
    except TraceStorageError as exc:
        _fail(f"trace bundle unreadable: {exc}")
        return

    recorded = recorded_output(trace)
    if recorded is not None:
        recorded = redact_json(recorded).value
    battery = derive_battery(expectation, recorded, has_expectation=has_expectation)
    score = score_expectation(expectation, battery, has_expectation=has_expectation)
    coverage = (
        pin_coverage(expectation, recorded)
        if recorded is not None and has_expectation
        else None
    )

    if as_json:
        # Plain stdout, not the rich console: markup would be interpreted and
        # a wrapped line is no longer one JSON document.
        typer.echo(
            json.dumps(
                _as_json(score, coverage, test, destination),
                indent=2,
                sort_keys=False,
            )
        )
    else:
        _report(score, coverage, test, destination)
    raise typer.Exit(_exit_status(score))


def _exit_status(score: ExpectationScore) -> int:
    if score.verdict == "claim_pinned":
        return EXIT_ANCHORED
    if score.verdict == "run_pinned":
        return EXIT_RUN_PINNED
    return EXIT_CANNOT_SCORE


def _report(
    score: ExpectationScore,
    coverage: PinCoverage | None,
    test: Path,
    bundle: Path,
) -> None:
    """Print the battery and its verdict."""
    if score.verdict == "no_expectation":
        console.print("[yellow]no expectation to score[/yellow]")
        console.print(
            f"{test} asserts the recorded run without an --expect-output; "
            "there is no claim for a mutant to separate from it."
        )
        return

    claim = [mutant for mutant in score.mutants if mutant.kind == "claim"]
    code = [mutant for mutant in score.mutants if mutant.kind == "code"]
    console.print(
        f"battery: {len(claim)} claim-drawn + {len(code)} code-drawn mutant(s), "
        f"derived from {test.name} and {bundle.name}"
    )
    console.print("")
    for mutant in score.mutants:
        outcome = "killed" if mutant.killed else "SURVIVED"
        location = (
            f"  (first difference at {mutant.differing_path})"
            if mutant.differing_path
            else ""
        )
        console.print(f"  {mutant.kind:<5} {mutant.name:<36} {outcome}{location}")
    console.print("")

    if score.verdict == "run_pinned":
        console.print(f"[red]{score.summary()}[/red]")
        console.print(
            "The generated test passes against the code that produced the "
            "recording. Treat the expectation as an unapproved oracle."
        )
    elif score.verdict == "undetermined":
        console.print(f"[yellow]{score.summary()}[/yellow]")
        console.print(
            "No recorded run output was available, so no claim-drawn mutant "
            "could be derived. Re-record the trace, or score against a bundle "
            "that carries a completed run."
        )
    else:
        console.print(f"[green]{score.summary()}[/green]")
        console.print(
            "The expectation rejects the run it was drawn from, so it encodes "
            "the claim rather than the recording."
        )

    if coverage is not None:
        console.print(
            f"pins: {len(coverage.live)} live ({_joined(coverage.live)}); "
            f"{len(coverage.reproduced)} leaf(s) reproduce the run "
            f"({_joined(coverage.reproduced)})"
        )


def _joined(paths: tuple[str, ...]) -> str:
    return ", ".join(paths) if paths else "none"


def _as_json(
    score: ExpectationScore,
    coverage: PinCoverage | None,
    test: Path,
    bundle: Path,
) -> dict[str, JsonValue]:
    """Return the score as a JSON-shaped payload."""
    return {
        "test": str(test),
        "bundle": str(bundle),
        "verdict": score.verdict,
        "killed": score.killed,
        "total": score.total,
        "discrimination": score.discrimination,
        "mutants": [
            {
                "name": mutant.name,
                "kind": mutant.kind,
                "killed": mutant.killed,
                "differing_path": mutant.differing_path,
            }
            for mutant in score.mutants
        ],
        "pins": (
            {
                "live": list(coverage.live),
                "reproduced": list(coverage.reproduced),
            }
            if coverage is not None
            else None
        ),
    }


def _load_json(path: Path) -> JsonValue | None:
    """Read an expectation file, reporting a read failure as a cannot-score."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"cannot read {path}: {exc}")
        return None
    except json.JSONDecodeError as exc:
        _fail(f"{path} is not valid JSON: {exc.msg}")
        return None
    return redact_json(payload).value


def _fail(message: str) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(EXIT_CANNOT_SCORE)
