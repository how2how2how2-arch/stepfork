"""Data models for fork-at-step operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from stepfork.trace.models import JsonValue


class ForkInfo(BaseModel):
    """Provenance metadata for a forked trace."""

    model_config = ConfigDict(extra="forbid")

    parent_trace_path: str | None = Field(default=None, min_length=1)
    parent_run_id: str = Field(min_length=1)
    fork_point_event_id: str = Field(min_length=1)
    fork_point_step: int = Field(ge=0)
    fork_timestamp: datetime
    overrides_applied: int = Field(default=0, ge=0)


class OverrideSpec(BaseModel):
    """Specification for overriding an event's output during replay.

    Overrides target specific events in the trace by step number or event ID,
    allowing you to modify recorded outputs before continuing execution.
    """

    model_config = ConfigDict(extra="forbid")

    step: int | None = Field(default=None, ge=0)
    event_id: str | None = Field(default=None, min_length=1)
    output: JsonValue = Field(..., description="New output value to use")

    def __init__(self, **data: object) -> None:
        """Validate that exactly one target is specified."""
        super().__init__(**data)
        if self.step is None and self.event_id is None:
            raise ValueError("override requires either step or event_id")
        if self.step is not None and self.event_id is not None:
            raise ValueError("override cannot specify both step and event_id")


@dataclass(frozen=True)
class ForkPoint:
    """Identifies where to fork a trace."""

    event_id: str | None
    step: int | None

    def __post_init__(self) -> None:
        """Validate that at least one identifier is provided."""
        if self.event_id is None and self.step is None:
            raise ValueError("fork point requires either event_id or step")


@dataclass(frozen=True)
class ForkResult:
    """Result of a fork operation."""

    success: bool
    forked_trace_path: str | None
    original_event_count: int
    forked_event_count: int
    fork_point_step: int
    fork_point_event_id: str
    overrides_applied: int
    overridden_steps: tuple[int, ...] = ()
    stale_event_ids: tuple[str, ...] = ()
    error_message: str | None = None


class ForkError(Exception):
    """Raised when a fork operation fails."""
