"""Instrumentation coverage, offline.

Builds two bundles from the same agent shape -- one whose dependency calls were
all captured, one where a recorded call has no captured outcome -- and prints the
coverage report for each. No network, no model, no clock.

Run:

    uv run python examples/coverage/demo.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from stepfork import ToolCall, Trace
from stepfork.inspect.coverage import coverage_bundle
from stepfork.trace import RunEnd, RunStart, RunStatus, ToolResult


def build(destination: Path, *, drop_last_result: bool) -> Path:
    """Record a two-call agent, optionally leaving the last call uncaptured."""
    trace = Trace(agent_name="coverage-demo")
    start = trace.add(RunStart(input={"prompt": "rebook the flight"}))
    search = trace.add(
        ToolCall(name="search", input={"query": "Kathmandu"}, parent_id=start.id)
    )
    search_result = trace.add(
        ToolResult(name="search", output={"flights": 3}, parent_id=search.id)
    )
    charge = trace.add(
        ToolCall(name="charge", input={"amount": 420}, parent_id=search_result.id)
    )
    if drop_last_result:
        # The call is in the trace; its outcome was never captured.
        trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=charge.id))
    else:
        result = trace.add(
            ToolResult(name="charge", output={"captured": True}, parent_id=charge.id)
        )
        trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=result.id))
    return trace.save(destination, status=RunStatus.COMPLETED, overwrite=True)


def describe(label: str, path: Path) -> None:
    report = coverage_bundle(path)
    rate = report.capture_rate()
    print(f"[{label}] {path.name}")
    print(f"   observed dependency calls: {report.observed_calls}")
    print(f"   captured:                  {report.captured_calls}")
    print(f"   unfingerprinted:           {report.unfingerprinted_calls}")
    print(f"   missing:                   {report.missing_calls}")
    if rate is None:
        print("   capture rate:              NOT MEASURED (no dependency calls)")
    else:
        print(
            f"   capture rate:              {report.captured_calls}/"
            f"{report.observed_calls} ({rate:.0%})"
        )
    unmeasurable = report.uninstrumented_calls
    print(f"   uninstrumented:            not measurable ({unmeasurable!r})")
    print(f"      {report.uninstrumented_reason}")
    print()


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        complete = build(root / "complete.sftrace", drop_last_result=False)
        partial = build(root / "partial.sftrace", drop_last_result=True)
        print("Instrumentation coverage, offline")
        print()
        describe("every call captured", complete)
        describe("one call uncaptured", partial)
        print(
            "Reading: coverage counts the calls the bundle holds. It never says how "
            "many calls the agent made, because a call made without instrumentation "
            "leaves nothing to count."
        )


if __name__ == "__main__":
    main()
