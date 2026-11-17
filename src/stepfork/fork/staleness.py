"""Classify recorded outputs that an override may have invalidated.

A fork keeps every event at or before the fork point, and an override replaces
one of those outputs. A recorded output that was produced after the overridden
step was captured under the upstream state the override changed, so substituting
it during replay is not evidence for the modified execution path.

This module names those outputs. It deliberately reports an upper bound rather
than a dependency analysis: every recorded output after the earliest overridden
step is reported, whether or not its inputs actually changed. Working out which
downstream events really depend on the changed value is a separate, harder
problem and is not attempted here.
"""

from __future__ import annotations

from dataclasses import dataclass

from stepfork.fork.overrides import OverrideRegistry
from stepfork.trace.models import EventType, Trace

# The event types whose output replay substitutes from the recording.
RECORDED_OUTPUT_TYPES = frozenset({EventType.TOOL_RESULT, EventType.LLM_RESPONSE})


@dataclass(frozen=True)
class StaleOutputs:
    """Which retained recorded outputs an override may have invalidated."""

    overridden_steps: tuple[int, ...]
    """Steps whose recorded output the fork replaced."""

    stale_event_ids: tuple[str, ...]
    """Retained recorded outputs recorded after the earliest overridden step."""

    @property
    def count(self) -> int:
        """Return how many retained outputs are potentially stale."""
        return len(self.stale_event_ids)


def classify_stale_outputs(trace: Trace, overrides: OverrideRegistry) -> StaleOutputs:
    """Report the retained outputs an upstream override may have invalidated.

    Args:
        trace: The forked trace, after its overrides have been applied.
        overrides: The registry used to fork it.

    Returns:
        The overridden steps, and the ids of the retained recorded outputs that
        were recorded after the earliest of them. An event the fork replaced is
        not reported as stale: its value is the override, not a reused output.
    """
    overridden_steps = sorted(
        {
            event.step
            for event in trace.events
            if overrides.get_override(event) is not None
        }
    )
    if not overridden_steps:
        return StaleOutputs(overridden_steps=(), stale_event_ids=())

    first_overridden_step = overridden_steps[0]
    stale_event_ids = tuple(
        event.id
        for event in trace.events
        if event.type in RECORDED_OUTPUT_TYPES
        and event.step > first_overridden_step
        and overrides.get_override(event) is None
    )
    return StaleOutputs(
        overridden_steps=tuple(overridden_steps),
        stale_event_ids=stale_event_ids,
    )
