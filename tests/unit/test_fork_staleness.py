"""Unit tests for the stale-output classification a fork reports.

The classification exists so that a recorded output produced under an upstream
state a fork changed is not indistinguishable from a verified result. These
tests run offline and build their traces in memory.
"""

from __future__ import annotations

from stepfork import ToolCall, Trace
from stepfork.fork import ForkPoint, OverrideSpec, fork_trace
from stepfork.trace import RunEnd, RunStart, RunStatus, ToolResult
from stepfork.trace.models import EventType

# Event steps in the trace below: 0 run_start, 1/2 search, 3/4 book, 5/6 email,
# 7 run_end. Every tool result therefore sits at an even step >= 2.
SEARCH_RESULT_STEP = 2
BOOK_RESULT_STEP = 4
EMAIL_RESULT_STEP = 6


def build_trace_with_three_tool_calls() -> Trace:
    """Build a completed run with three tool calls, each with a result."""
    trace = Trace(agent_name="test-agent", status=RunStatus.COMPLETED)
    start = trace.add(RunStart(input={"query": "test"}))
    parent_id = start.id
    calls = (
        ("search", {"count": 5}),
        ("book", {"status": "confirmed"}),
        ("email", {"status": "sent"}),
    )
    for name, output in calls:
        call = trace.add(ToolCall(name=name, input={"q": name}, parent_id=parent_id))
        result = trace.add(ToolResult(name=name, output=output, parent_id=call.id))
        parent_id = result.id
    trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=parent_id))
    return trace


def event_id_at_step(trace: Trace, step: int) -> str:
    """Return the id of the event at ``step``.

    A fork preserves event ids, so an id read from the parent trace is the id
    the same event carries in the forked trace.
    """
    return next(event.id for event in trace.events if event.step == step)


def test_no_overrides_report_nothing_stale() -> None:
    """A fork that changes nothing must not claim anything is stale."""
    trace = build_trace_with_three_tool_calls()

    result = fork_trace(
        trace,
        ForkPoint(event_id=None, step=EMAIL_RESULT_STEP),
    )

    assert result.success is True
    assert result.overridden_steps == ()
    assert result.stale_event_ids == ()


def test_outputs_after_an_override_are_reported_stale() -> None:
    """Every recorded output after the overridden step is reported stale."""
    trace = build_trace_with_three_tool_calls()
    expected = (
        event_id_at_step(trace, BOOK_RESULT_STEP),
        event_id_at_step(trace, EMAIL_RESULT_STEP),
    )

    result = fork_trace(
        trace,
        ForkPoint(event_id=None, step=EMAIL_RESULT_STEP),
        overrides=[
            OverrideSpec(step=SEARCH_RESULT_STEP, output={"count": 99}),
        ],
    )

    assert result.overridden_steps == (SEARCH_RESULT_STEP,)
    assert result.stale_event_ids == expected


def test_the_overridden_output_is_not_stale_itself() -> None:
    """The replaced output is the override, not a reused recorded output."""
    trace = build_trace_with_three_tool_calls()

    result = fork_trace(
        trace,
        ForkPoint(event_id=None, step=SEARCH_RESULT_STEP),
        overrides=[
            OverrideSpec(step=SEARCH_RESULT_STEP, output={"count": 99}),
        ],
    )

    assert result.overridden_steps == (SEARCH_RESULT_STEP,)
    assert result.stale_event_ids == ()


def test_only_outputs_after_the_earliest_override_are_stale() -> None:
    """An override late in the prefix must not mark earlier outputs stale."""
    trace = build_trace_with_three_tool_calls()

    result = fork_trace(
        trace,
        ForkPoint(event_id=None, step=EMAIL_RESULT_STEP),
        overrides=[
            OverrideSpec(step=BOOK_RESULT_STEP, output={"status": "rejected"}),
        ],
    )

    assert result.overridden_steps == (BOOK_RESULT_STEP,)
    assert result.stale_event_ids == (event_id_at_step(trace, EMAIL_RESULT_STEP),)


def test_two_overrides_report_the_steps_not_a_count() -> None:
    """The report carries which steps were overridden, not how many."""
    trace = build_trace_with_three_tool_calls()

    result = fork_trace(
        trace,
        ForkPoint(event_id=None, step=EMAIL_RESULT_STEP),
        overrides=[
            OverrideSpec(step=SEARCH_RESULT_STEP, output={"count": 99}),
            OverrideSpec(step=EMAIL_RESULT_STEP, output={"status": "failed"}),
        ],
    )

    assert result.overrides_applied == 2
    assert result.overridden_steps == (SEARCH_RESULT_STEP, EMAIL_RESULT_STEP)
    assert result.stale_event_ids == (event_id_at_step(trace, BOOK_RESULT_STEP),)


def test_calls_are_never_classified_as_stale() -> None:
    """Only recorded outputs are classified; the calls that produced them are not."""
    trace = build_trace_with_three_tool_calls()
    call_ids = {event.id for event in trace.events if event.type is EventType.TOOL_CALL}

    result = fork_trace(
        trace,
        ForkPoint(event_id=None, step=EMAIL_RESULT_STEP),
        overrides=[
            OverrideSpec(step=SEARCH_RESULT_STEP, output={"count": 99}),
        ],
    )

    assert set(result.stale_event_ids).isdisjoint(call_ids)
    assert len(result.stale_event_ids) == 2
