from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from stepfork import ToolCall, Trace
from stepfork.cli.main import app
from stepfork.inspect.coverage import (
    CaptureState,
    coverage_bundle,
    instrumentation_coverage,
)
from stepfork.trace import RunEnd, RunStart, RunStatus, ToolResult

runner = CliRunner()


def complete_trace() -> Trace:
    """A trace whose single dependency call is captured and fingerprinted."""
    trace = Trace(agent_name="demo-agent")
    start = trace.add(RunStart(input={"prompt": "book"}))
    call = trace.add(
        ToolCall(name="search", input={"query": "Kathmandu"}, parent_id=start.id)
    )
    result = trace.add(
        ToolResult(name="search", output={"ok": True}, parent_id=call.id)
    )
    trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=result.id))
    return trace


def trace_with_uncaptured_call() -> Trace:
    """Two calls, one of which has no captured outcome."""
    trace = Trace(agent_name="demo-agent")
    start = trace.add(RunStart(input={"prompt": "book"}))
    call = trace.add(
        ToolCall(name="search", input={"query": "Kathmandu"}, parent_id=start.id)
    )
    result = trace.add(
        ToolResult(name="search", output={"ok": True}, parent_id=call.id)
    )
    # A recorded call with no paired result: the outcome was never captured.
    tail = trace.add(ToolCall(name="charge", input={"amount": 10}, parent_id=result.id))
    trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=tail.id))
    return trace


def save(trace: Trace, tmp_path: Path, name: str = "demo.sftrace") -> Path:
    return trace.save(tmp_path / name, status=RunStatus.COMPLETED)


def test_captured_call_reports_full_coverage(tmp_path: Path) -> None:
    path = save(complete_trace(), tmp_path)

    report = coverage_bundle(path)

    assert report.observed_calls == 1
    assert report.captured_calls == 1
    assert report.missing_calls == 0
    assert report.capture_rate() == 1.0
    assert [call.state for call in report.calls] == [CaptureState.CAPTURED]


def test_uncaptured_call_is_counted_not_dropped(tmp_path: Path) -> None:
    """A call with no outcome must stay in the denominator.

    Counting only the calls that happen to have an outcome would report perfect
    coverage for a bundle that cannot replay half of it.
    """
    path = save(trace_with_uncaptured_call(), tmp_path, "partial.sftrace")

    report = coverage_bundle(path)

    assert report.observed_calls == 2
    assert report.captured_calls == 1
    assert report.missing_calls == 1
    assert report.capture_rate() == 0.5
    assert report.calls[1].state is CaptureState.MISSING


def test_unfingerprinted_outcome_is_not_counted_as_captured(tmp_path: Path) -> None:
    """An outcome without a digest cannot be verified, so it is not coverage."""
    path = save(complete_trace(), tmp_path, "nofingerprint.sftrace")
    trace = Trace.load(path)
    stripped = Trace(agent_name=trace.agent_name)
    for event in trace.events:
        stripped.events.append(
            event.model_copy(
                update={"input_hash": None, "output_hash": None}, deep=True
            )
        )

    report = instrumentation_coverage(stripped)

    assert report.observed_calls == 1
    assert report.unfingerprinted_calls == 1
    assert report.captured_calls == 0
    assert report.input_fingerprinted_calls == 0
    assert report.capture_rate() == 0.0


def test_uninstrumented_calls_are_reported_as_unmeasurable(tmp_path: Path) -> None:
    """The count StepFork cannot see must not be published as a zero."""
    report = coverage_bundle(save(complete_trace(), tmp_path))

    assert report.uninstrumented_calls is None
    assert "cannot be counted" in report.uninstrumented_reason


def test_empty_trace_reports_no_capture_rate(tmp_path: Path) -> None:
    """An empty denominator is not a rate of zero."""
    trace = Trace(agent_name="empty-agent")
    start = trace.add(RunStart(input={"prompt": "nothing"}))
    trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=start.id))

    report = instrumentation_coverage(trace)

    assert report.observed_calls == 0
    assert report.capture_rate() is None


def test_cli_prints_the_report(tmp_path: Path) -> None:
    path = save(trace_with_uncaptured_call(), tmp_path, "partial.sftrace")

    result = runner.invoke(app, ["coverage", str(path)])

    assert result.exit_code == 0
    assert "Observed calls" in result.stdout
    assert "Capture rate: 1/2" in result.stdout
    assert "not measurable" in result.stdout


def test_cli_json_matches_the_report(tmp_path: Path) -> None:
    path = save(trace_with_uncaptured_call(), tmp_path, "partial.sftrace")

    result = runner.invoke(app, ["coverage", str(path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["observed_calls"] == 2
    assert payload["missing_calls"] == 1
    assert payload["uninstrumented_calls"] is None


def test_cli_lists_calls_when_asked(tmp_path: Path) -> None:
    path = save(trace_with_uncaptured_call(), tmp_path, "partial.sftrace")

    result = runner.invoke(app, ["coverage", str(path), "--calls"])

    assert result.exit_code == 0
    assert "charge" in result.stdout
