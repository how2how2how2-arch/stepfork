"""Minimization orchestrator that coordinates the entire minimization process."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from stepfork.export.entrypoint import resolve_entrypoint
from stepfork.minimize.delta_debug import (
    DeltaDebugger,
    EventSubset,
    create_minimized_trace,
)
from stepfork.minimize.models import (
    FailureSignature,
    MinimizationReport,
    MinimizationResult,
)
from stepfork.minimize.signature import (
    ReplayNeverReachedRecordedCall,
    detect_failure_signature,
    format_signature,
)
from stepfork.trace.models import JsonValue, Trace


class MinimizationError(Exception):
    """Raised when minimization cannot proceed."""


class TimeoutError(MinimizationError):
    """Raised when a replay attempt times out."""


class MinimizationEngine:
    """Orchestrates the trace minimization process.

    Coordinates failure detection, delta debugging, timeout handling,
    and nondeterminism detection.
    """

    def __init__(
        self,
        trace: Trace,
        entrypoint: str,
        *,
        expected_output: JsonValue | None = None,
        mode: str = "frozen",
        timeout_seconds: float = 30.0,
        max_attempts_per_test: int = 1,
    ):
        """Initialize the minimization engine.

        Args:
            trace: The original trace to minimize
            entrypoint: MODULE:FUNCTION entrypoint specification
            expected_output: Expected output for comparison (optional)
            mode: Replay mode (frozen, live, hybrid)
            timeout_seconds: Maximum time per replay attempt
            max_attempts_per_test: Number of attempts to verify each subset
        """
        self.trace = trace
        self.entrypoint = entrypoint
        self.expected_output = expected_output
        self.mode = mode
        self.timeout_seconds = timeout_seconds
        self.max_attempts_per_test = max_attempts_per_test

        # Statistics
        self.total_replay_attempts = 0
        self.nondeterministic_failures = 0

        # Resolve entrypoint once
        try:
            self.entrypoint_fn = resolve_entrypoint(entrypoint, import_root=Path.cwd())
        except Exception as exc:
            raise MinimizationError(
                f"cannot resolve entrypoint {entrypoint!r}: {exc}"
            ) from exc

    def minimize(self) -> MinimizationResult:
        """Execute the minimization process.

        Returns:
            MinimizationResult describing the outcome
        """
        # Step 1: Detect the original failure signature
        try:
            try:
                from stepfork.trace.replay_policy import ReplayPolicy

                ReplayPolicy(self.mode)
            except ValueError as exc:
                return MinimizationResult(
                    success=False,
                    original_event_count=len(self.trace.events),
                    minimized_event_count=None,
                    replay_attempts=self.total_replay_attempts,
                    preserved_failure=False,
                    is_minimal=False,
                    error_message=f"Invalid replay mode {self.mode!r}: {exc}",
                )
            original_signature = detect_failure_signature(
                self.trace,
                self.entrypoint,
                expected_output=self.expected_output,
                mode=self.mode,
                timeout_seconds=self.timeout_seconds,
            )
            self.total_replay_attempts += 1
        except ReplayNeverReachedRecordedCall:
            # A refusal, not a failed minimization: the caller must be able to
            # tell them apart by exit status, so it travels untouched.
            raise
        except Exception as exc:
            return MinimizationResult(
                success=False,
                original_event_count=len(self.trace.events),
                minimized_event_count=None,
                replay_attempts=self.total_replay_attempts,
                preserved_failure=False,
                is_minimal=False,
                error_message=f"Failed to detect original failure signature: {exc}",
            )

        # Step 2: Run delta debugging
        debugger = DeltaDebugger(
            self.trace,
            predicate=lambda subset: self._test_subset_reproduces_failure(
                subset, original_signature
            ),
            preserve_run_markers=True,
        )

        try:
            minimal_subset = debugger.minimize()
            self.total_replay_attempts += debugger.attempts
        except Exception as exc:
            return MinimizationResult(
                success=False,
                original_event_count=len(self.trace.events),
                minimized_event_count=None,
                replay_attempts=self.total_replay_attempts,
                preserved_failure=False,
                is_minimal=False,
                failure_signature=original_signature,
                error_message=f"Delta debugging failed: {exc}",
            )

        # Step 3: Verify the minimized subset still reproduces the failure
        minimized_trace = create_minimized_trace(self.trace, minimal_subset)
        final_check = self._verify_failure_signature(
            minimized_trace, original_signature
        )
        self.total_replay_attempts += 1

        if not final_check:
            return MinimizationResult(
                success=False,
                original_event_count=len(self.trace.events),
                minimized_event_count=len(minimized_trace.events),
                replay_attempts=self.total_replay_attempts,
                preserved_failure=False,
                is_minimal=False,
                failure_signature=original_signature,
                error_message="Minimized trace does not reproduce the original failure",
            )

        # Success
        return MinimizationResult(
            success=True,
            original_event_count=len(self.trace.events),
            minimized_event_count=len(minimized_trace.events),
            replay_attempts=self.total_replay_attempts,
            preserved_failure=True,
            is_minimal=len(minimized_trace.events) < len(self.trace.events),
            failure_signature=original_signature,
            nondeterministic=self.nondeterministic_failures > 0,
        )

    def _test_subset_reproduces_failure(
        self,
        subset: EventSubset,
        target_signature: FailureSignature,
    ) -> bool:
        """Test if an event subset reproduces the target failure signature.

        Returns True if the subset fails with the same signature.
        """
        if not subset.includes_dependency_calls:
            # Subsets without any dependency calls cannot reproduce execution failures
            return False

        # Create a temporary trace from the subset
        temp_trace = create_minimized_trace(self.trace, subset)

        # Test multiple times if configured (for nondeterminism detection)
        successes = 0
        for _ in range(self.max_attempts_per_test):
            if self._verify_failure_signature(temp_trace, target_signature):
                successes += 1

        # Detect nondeterminism
        if 0 < successes < self.max_attempts_per_test:
            self.nondeterministic_failures += 1

        # Require all attempts to succeed for reproducibility
        return successes == self.max_attempts_per_test

    def _verify_failure_signature(
        self,
        trace: Trace,
        target_signature: FailureSignature,
    ) -> bool:
        """Verify that a trace produces the target failure signature."""
        try:
            with _timeout(self.timeout_seconds):
                actual_signature = detect_failure_signature(
                    trace,
                    self.entrypoint,
                    expected_output=self.expected_output,
                    mode=self.mode,
                    timeout_seconds=self.timeout_seconds,
                )
                return target_signature.matches(actual_signature)
        except TimeoutError:
            # Timeout counts as not matching the signature
            return False
        except Exception:
            # Any other error counts as not matching
            return False


@contextmanager
def _timeout(seconds: float) -> Iterator[None]:
    """Context manager for timeout enforcement."""

    def timeout_handler(signum: int, frame: Any) -> None:
        raise TimeoutError(f"Operation timed out after {seconds} seconds")

    # Only works on Unix-like systems
    if hasattr(signal, "SIGALRM"):
        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
    else:
        # On Windows, just skip timeout enforcement
        yield


def generate_report(
    result: MinimizationResult,
    original_trace_path: str,
    minimized_trace_path: str | None,
) -> MinimizationReport:
    """Generate a human-readable minimization report."""
    warnings: list[str] = []

    if result.nondeterministic:
        warnings.append(
            "Nondeterministic failures detected during minimization. "
            "Results may vary across runs."
        )

    if not result.is_minimal and result.success:
        warnings.append("Trace was already minimal or could not be reduced further.")

    reduction_percentage: float | None = None
    if result.minimized_event_count is not None and result.original_event_count > 0:
        reduction = result.original_event_count - result.minimized_event_count
        reduction_percentage = (reduction / result.original_event_count) * 100

    signature_str = (
        format_signature(result.failure_signature)
        if result.failure_signature
        else "Unknown"
    )

    return MinimizationReport(
        original_trace_path=original_trace_path,
        minimized_trace_path=minimized_trace_path,
        original_event_count=result.original_event_count,
        minimized_event_count=result.minimized_event_count,
        reduction_percentage=reduction_percentage,
        replay_attempts=result.replay_attempts,
        failure_signature=signature_str,
        is_minimal=result.is_minimal,
        nondeterministic=result.nondeterministic,
        warnings=warnings,
    )
