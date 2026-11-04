"""Instrumentation coverage reported from what a trace can observe.

The report answers one question and refuses a larger one. It counts the
dependency calls the trace actually holds and says, per call, how much evidence
the bundle carries for it. It does not report how many calls the run made: a
call StepFork never instrumented leaves no event behind, so those calls are
absent from the denominator by construction, and this module reports that
absence as unmeasurable rather than as zero.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from stepfork.replay.plan import RecordedCall, extract_recorded_calls
from stepfork.trace.models import Trace

UNMEASURABLE_REASON = (
    "StepFork observes only the dependency calls its own instrumentation "
    "recorded. A call made outside that instrumentation writes no event, so the "
    "number of uninstrumented calls cannot be counted from a bundle. The field "
    "stays null rather than 0 so it cannot be read as a measured zero."
)


class CaptureState(StrEnum):
    """How much evidence a bundle carries for one recorded call."""

    CAPTURED = "captured"
    """The outcome is present and carries a digest that replay can verify."""

    UNFINGERPRINTED = "unfingerprinted"
    """The outcome is present but carries no digest, so it cannot be verified."""

    MISSING = "missing"
    """The call was recorded but no outcome was captured for it."""


class CallCoverage(BaseModel):
    """Coverage for a single recorded dependency call."""

    model_config = ConfigDict(extra="forbid")

    index: int
    kind: Literal["tool", "llm"]
    label: str
    state: CaptureState
    input_fingerprinted: bool


class CoverageReport(BaseModel):
    """Instrumentation coverage for one trace."""

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    agent_name: str
    run_id: str
    observed_calls: int
    tool_calls: int
    llm_calls: int
    captured_calls: int
    unfingerprinted_calls: int
    missing_calls: int
    input_fingerprinted_calls: int
    uninstrumented_calls: Literal[None] = None
    uninstrumented_reason: str = UNMEASURABLE_REASON
    calls: list[CallCoverage]

    def capture_rate(self) -> float | None:
        """Share of observed calls whose outcome carries a verifiable digest.

        Returns ``None`` when the trace observed no dependency calls. An empty
        denominator is not a rate of zero, and reporting it as ``0.0`` would
        announce a measurement where none was taken.
        """
        if self.observed_calls == 0:
            return None
        return self.captured_calls / self.observed_calls


def coverage_bundle(path: str | Path) -> CoverageReport:
    """Load a `.sftrace` bundle and report its instrumentation coverage."""
    bundle_path = Path(path)
    return instrumentation_coverage(Trace.load(bundle_path), path=bundle_path)


def instrumentation_coverage(
    trace: Trace,
    *,
    path: str | Path | None = None,
) -> CoverageReport:
    """Report coverage for the dependency calls ``trace`` actually observed."""
    recorded = extract_recorded_calls(trace)
    calls = [_call_coverage(call) for call in recorded]
    return CoverageReport(
        path=str(path) if path is not None else None,
        agent_name=trace.agent_name,
        run_id=trace.run_id,
        observed_calls=len(calls),
        tool_calls=sum(1 for call in calls if call.kind == "tool"),
        llm_calls=sum(1 for call in calls if call.kind == "llm"),
        captured_calls=sum(1 for call in calls if call.state is CaptureState.CAPTURED),
        unfingerprinted_calls=sum(
            1 for call in calls if call.state is CaptureState.UNFINGERPRINTED
        ),
        missing_calls=sum(1 for call in calls if call.state is CaptureState.MISSING),
        input_fingerprinted_calls=sum(1 for call in calls if call.input_fingerprinted),
        calls=calls,
    )


def _call_coverage(call: RecordedCall) -> CallCoverage:
    return CallCoverage(
        index=call.index,
        kind=call.kind,
        label=call.label,
        state=_capture_state(call),
        input_fingerprinted=call.input_hash is not None,
    )


def _capture_state(call: RecordedCall) -> CaptureState:
    if call.missing:
        return CaptureState.MISSING
    if call.output_hash is None:
        return CaptureState.UNFINGERPRINTED
    return CaptureState.CAPTURED
