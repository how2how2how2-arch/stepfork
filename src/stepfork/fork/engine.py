"""Fork engine for creating forked traces at specific steps."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from stepfork.fork.models import (
    ForkError,
    ForkInfo,
    ForkPoint,
    ForkResult,
    OverrideSpec,
)
from stepfork.fork.overrides import OverrideRegistry
from stepfork.fork.staleness import classify_stale_outputs
from stepfork.trace.models import Event, Trace


class ForkEngine:
    """Orchestrates forking a trace at a specific step."""

    def __init__(
        self,
        trace: Trace,
        fork_point: ForkPoint,
        *,
        overrides: list[OverrideSpec] | None = None,
        parent_trace_path: str | None = None,
    ):
        """Initialize the fork engine.

        Args:
            trace: The original trace to fork
            fork_point: Where to fork (event ID or step number)
            overrides: Optional list of overrides to apply
            parent_trace_path: Optional path to parent trace for provenance
        """
        self.trace = trace
        self.fork_point = fork_point
        self.overrides = overrides or []
        self.parent_trace_path = parent_trace_path

    def fork(self) -> tuple[Trace, ForkInfo]:
        """Create a forked trace at the specified fork point.

        Returns:
            Tuple of (forked_trace, fork_info)

        Raises:
            ForkError: If fork operation fails
        """
        # Resolve fork point to event ID and step
        fork_event_id, fork_step = self._resolve_fork_point()

        # Extract events before fork point
        events_before_fork = self._extract_events_before_fork(fork_step)

        # Validate structural integrity
        self._validate_fork_point(events_before_fork, fork_step)

        # Validate and prepare overrides
        override_registry = OverrideRegistry(self.overrides)
        override_registry.validate_overrides(events_before_fork, strict=True)

        # Create new trace with preserved events and applied overrides
        forked_trace = self._create_forked_trace(events_before_fork, override_registry)

        # Count actually applied overrides
        applied_count = sum(
            1 for event in events_before_fork if override_registry.get_override(event)
        )

        # Create fork info for provenance
        fork_info = ForkInfo(
            parent_trace_path=self.parent_trace_path,
            parent_run_id=self.trace.run_id,
            fork_point_event_id=fork_event_id,
            fork_point_step=fork_step,
            fork_timestamp=datetime.now(UTC),
            overrides_applied=applied_count,
        )

        return forked_trace, fork_info

    def _resolve_fork_point(self) -> tuple[str, int]:
        """Resolve fork point to both event ID and step number.

        Returns:
            Tuple of (event_id, step)

        Raises:
            ForkError: If fork point cannot be resolved
        """
        if self.fork_point.event_id is not None:
            # Find event by ID
            for event in self.trace.events:
                if event.id == self.fork_point.event_id:
                    return event.id, event.step
            raise ForkError(f"event ID {self.fork_point.event_id!r} not found in trace")

        if self.fork_point.step is not None:
            # Find event by step
            for event in self.trace.events:
                if event.step == self.fork_point.step:
                    return event.id, event.step
            raise ForkError(f"step {self.fork_point.step} not found in trace")

        raise ForkError("fork point must specify either event_id or step")

    def _extract_events_before_fork(self, fork_step: int) -> list[Event]:
        """Extract all events before the fork point.

        Args:
            fork_step: The step number at which to fork

        Returns:
            List of events occurring before fork_step (inclusive)
        """
        # Include all events up to and including the fork step
        return [event for event in self.trace.events if event.step <= fork_step]

    def _validate_fork_point(self, events_before: list[Event], fork_step: int) -> None:
        """Validate that fork point preserves structural integrity.

        Args:
            events_before: Events that will be in the forked trace
            fork_step: The fork point step

        Raises:
            ForkError: If fork would break structural integrity
        """
        # Check that we have at least one event
        if not events_before:
            raise ForkError("cannot fork at step before any events")

        # Check that all parent references are satisfied
        event_ids_before = {event.id for event in events_before}

        for event in events_before:
            if event.parent_id is not None and event.parent_id not in event_ids_before:
                raise ForkError(
                    f"fork at step {fork_step} would break parent dependency: "
                    f"event {event.id} references parent {event.parent_id} "
                    "which occurs after fork point"
                )

    def _create_forked_trace(
        self, events_before: list[Event], override_registry: OverrideRegistry
    ) -> Trace:
        """Create a new trace with events before fork point.

        Args:
            events_before: Events to include in forked trace
            override_registry: Registry of overrides to apply

        Returns:
            New Trace instance with preserved events and applied overrides
        """
        # Create new trace with same agent name but new run_id
        forked_trace = Trace(
            agent_name=self.trace.agent_name,
            created_at=self.trace.created_at,
            status=self.trace.status,
            failure=self.trace.failure,
            environment=self.trace.environment,
        )

        # Add events with original IDs and metadata preserved, applying overrides
        for event in events_before:
            # Check for override
            override = override_registry.get_override(event)
            if override:
                # Apply override to event
                event = override_registry.apply_override(event, override)

            # Preserve original event, update run_id to new trace's run_id
            forked_event = event.model_copy(
                update={"run_id": forked_trace.run_id}, deep=True
            )
            # Manually append to maintain original step numbers and IDs
            forked_trace.events.append(forked_event)

        # Update totals
        forked_trace.totals = forked_trace._computed_totals()

        return forked_trace


def fork_trace(
    trace: Trace | Path,
    fork_point: ForkPoint,
    *,
    overrides: list[OverrideSpec] | None = None,
    output_path: Path | None = None,
    overwrite: bool = False,
) -> ForkResult:
    """Fork a trace at a specific step.

    Args:
        trace: Trace or path to trace to fork
        fork_point: Where to fork the trace
        overrides: Optional overrides to apply
        output_path: Optional path to save forked trace
        overwrite: Whether to overwrite existing output

    Returns:
        ForkResult with operation details

    Raises:
        ForkError: If fork operation fails
    """
    # Load trace if path provided
    if isinstance(trace, Path):
        parent_trace_path = str(trace)
        trace = Trace.load(trace)
    else:
        parent_trace_path = None

    original_event_count = len(trace.events)

    # Create fork engine
    engine = ForkEngine(
        trace,
        fork_point,
        overrides=overrides,
        parent_trace_path=parent_trace_path,
    )

    try:
        # Execute fork
        forked_trace, fork_info = engine.fork()

        # A recorded output after an overridden step was captured under the
        # upstream state the override changed, so substituting it during replay
        # is not evidence for the modified execution path. Name it here rather
        # than let it pass as an ordinary substitution.
        stale = classify_stale_outputs(forked_trace, OverrideRegistry(overrides))

        # Save if output path provided
        saved_path: str | None = None
        if output_path is not None:
            saved_path = str(forked_trace.save(output_path, overwrite=overwrite))

        return ForkResult(
            success=True,
            forked_trace_path=saved_path,
            original_event_count=original_event_count,
            forked_event_count=len(forked_trace.events),
            fork_point_step=fork_info.fork_point_step,
            fork_point_event_id=fork_info.fork_point_event_id,
            overrides_applied=fork_info.overrides_applied,
            overridden_steps=stale.overridden_steps,
            stale_event_ids=stale.stale_event_ids,
        )

    except Exception as exc:
        return ForkResult(
            success=False,
            forked_trace_path=None,
            original_event_count=original_event_count,
            forked_event_count=0,
            fork_point_step=fork_point.step or -1,
            fork_point_event_id=fork_point.event_id or "",
            overrides_applied=0,
            error_message=str(exc),
        )
