"""Tests for refusing a signature produced before any recorded call.

A trace records where it failed. When the recording fails after a dependency
call, a replay that consumes no recorded call never reached that place, so the
exception it raised is not the recording's failure -- and a bundle minimized
from it would be derived from something that did not happen. Exception type
cannot decide this: a setup error and a change that moves the failure earlier
look the same from inside the replay. See issue #22.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from stepfork import llm_request, record, trace_tool
from stepfork.cli.main import app
from stepfork.minimize.signature import (
    FailureDetectionError,
    ReplayNeverReachedRecordedCall,
    detect_failure_signature,
)
from stepfork.trace.models import Trace

runner = CliRunner()


@trace_tool(name="lookup_customer")
def lookup_customer(customer_id: str) -> dict[str, str]:
    """A dependency the recording calls once, before it fails."""
    return {"id": customer_id, "tier": "gold"}


def _plan() -> dict[str, str]:
    plan: dict[str, str] = llm_request(
        provider="fake",
        model="m",
        input={"messages": [{"role": "user", "content": "refund?"}]},
        call=lambda: {"content": "look up then refund", "model": "m"},
    )
    return plan


def _failing_agent() -> dict[str, str]:
    _plan()
    who = lookup_customer("cust-42")
    if who["tier"] == "gold":
        raise ValueError("refund path broken for gold tier")
    return {"ok": "true"}


@pytest.fixture()
def failing_trace(tmp_path: Path) -> Path:
    """A trace with two dependency calls before its recorded failure."""
    path = tmp_path / "two-call.sftrace"
    # Let the failure escape the recording block so the trace records it.
    try:
        with record("two-call-agent", output=str(path)) as session:
            session.set_output(_failing_agent())
    except ValueError:
        pass
    return path


# --- entrypoints exercised by the tests -------------------------------------


def imports_a_missing_dependency() -> None:
    # Dynamic on purpose: a literal import of a module that does not exist is
    # an error mypy would report for this file.
    importlib.import_module("definitely_not_installed_module")


def setup_error_with_the_recorded_type() -> None:
    raise ValueError("config file missing")


def fails_after_consuming_every_recorded_call() -> None:
    _plan()
    lookup_customer("cust-42")
    raise TypeError("refund path now fails differently")


def returns_without_failing() -> dict[str, str]:
    return {"ok": "true"}


def _run_cli(trace: Path, entrypoint: str, out: Path) -> Result:
    return runner.invoke(
        app,
        [
            "minimize",
            str(trace),
            "--entrypoint",
            f"test_minimize_unreached_call:{entrypoint}",
            "-o",
            str(out),
        ],
    )


# --- the refusal ------------------------------------------------------------


def test_setup_error_is_refused(failing_trace: Path) -> None:
    """An import error is not the recording's failure."""
    with pytest.raises(ReplayNeverReachedRecordedCall) as caught:
        detect_failure_signature(
            Trace.load(failing_trace),
            "test_minimize_unreached_call:imports_a_missing_dependency",
        )
    message = str(caught.value)
    assert "ModuleNotFoundError" in message
    assert "after 2 dependency call(s)" in message


def test_setup_error_of_the_recorded_type_is_refused(failing_trace: Path) -> None:
    """The recorded exception type does not make an unreached run faithful."""
    with pytest.raises(ReplayNeverReachedRecordedCall):
        detect_failure_signature(
            Trace.load(failing_trace),
            "test_minimize_unreached_call:setup_error_with_the_recorded_type",
        )


def test_a_refusal_is_a_detection_failure(failing_trace: Path) -> None:
    """Callers that already catch FailureDetectionError keep working."""
    with pytest.raises(FailureDetectionError):
        detect_failure_signature(
            Trace.load(failing_trace),
            "test_minimize_unreached_call:imports_a_missing_dependency",
        )


def test_cli_exits_two_and_writes_no_bundle(
    failing_trace: Path,
    tmp_path: Path,
) -> None:
    """The refusal is separable from a failed minimization by exit status."""
    out = tmp_path / "never.sftrace"
    result = _run_cli(failing_trace, "imports_a_missing_dependency", out)
    assert result.exit_code == 2
    assert "before reaching any recorded call" in result.stdout
    assert not out.exists()


# --- what must keep working -------------------------------------------------


def test_failure_after_consuming_the_recorded_calls_still_minimizes(
    failing_trace: Path,
    tmp_path: Path,
) -> None:
    """Same calls, different exception type: a real regression, not a refusal."""
    signature = detect_failure_signature(
        Trace.load(failing_trace),
        "test_minimize_unreached_call:fails_after_consuming_every_recorded_call",
    )
    assert signature.exception_type == "TypeError"


def test_cli_minimizes_a_failure_reached_through_the_recorded_calls(
    failing_trace: Path,
    tmp_path: Path,
) -> None:
    out = tmp_path / "reached.sftrace"
    result = _run_cli(
        failing_trace,
        "fails_after_consuming_every_recorded_call",
        out,
    )
    assert result.exit_code == 0
    assert out.exists()


def test_no_failure_at_all_keeps_its_own_exit_status(
    failing_trace: Path,
    tmp_path: Path,
) -> None:
    """A clean replay is "no failure to minimize" (exit 1), not a refusal."""
    out = tmp_path / "clean.sftrace"
    result = _run_cli(failing_trace, "returns_without_failing", out)
    assert result.exit_code == 1
    assert "no failure to minimize" in result.stdout
    assert not out.exists()


def test_a_trace_whose_failure_precedes_its_calls_is_not_refused(
    tmp_path: Path,
) -> None:
    """With no recorded call before the error there is nothing to fail to reach."""
    path = tmp_path / "early.sftrace"

    @trace_tool(name="unused_tool")
    def unused_tool(value: int) -> int:
        return value

    with record("early-failure-agent", output=str(path)) as session:
        session.set_output({"never": "reached"})
        session.record_error("StartupError", "the agent could not start")

    with pytest.raises(FailureDetectionError) as caught:
        detect_failure_signature(
            Trace.load(path),
            "test_minimize_unreached_call:returns_without_failing",
        )
    assert not isinstance(caught.value, ReplayNeverReachedRecordedCall)
    assert "no failure to minimize" in str(caught.value)
