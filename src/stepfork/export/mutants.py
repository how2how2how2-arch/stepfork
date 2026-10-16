"""Derive a mutant battery from an exported expectation and the recorded run.

:mod:`stepfork.export.scoring` scores a battery the caller supplies. Building
that battery by hand is the part that does not scale, and it is the part that
is easy to skip: an expectation that has drifted into a copy of the recorded
run looks exactly like an anchored one until a claim-drawn mutant separates
them. This module derives the battery mechanically from two things the export
already has.

Where the two mutants come from
-------------------------------

An exported test compares the replayed output against ``EXPECTATION``. Two
values are therefore already at hand when the export is prepared:

* the **expectation** -- the claim, as the generated test asserts it;
* the **recorded run's output** -- the ``RunEnd.output`` of the trace bundle,
  which is the value the code that produced the recording actually returned.

The claim-drawn mutant is that second value, verbatim: the run's own output is
the most plausible output of a variant implementation, because some
implementation did produce it. It is the row that discriminates. A
claim-pinned expectation rejects it -- the run it came from is the bug, so the
expectation must fail against it. A run-pinned expectation accepts it, and
that acceptance is the finding: the test would pass against the code that
produced the recording.

The code-drawn mutants are perturbations of the *claim's* own leaves -- an
integer moved by one, a string suffixed, a value replaced by ``None``. They
are controls, not discriminators: a run-pinned expectation rejects them too, so
they cannot separate the two readings by themselves. They are what makes the
reading legible, because the contrast between a row family that is always
killed and a row that survives is the whole measurement.

Two guards keep the families honest
-----------------------------------

* A perturbation that would land on the recorded value at that leaf is
  **skipped**. A row carrying the run's own value is a claim-drawn row wearing a
  code-drawn label, and it would let a run-pinned expectation read as though the
  battery had caught something.
* The claim-drawn row is the recorded output **whole**, never a projection of it
  onto the expectation's shape. The generated test compares whole values, and a
  comparison that ignores keys the claim does not name would accept an
  expectation that is *stricter* than the run -- the one reading this module
  must not produce.

Per-leaf attribution comes from :func:`pin_coverage`, which reports which
leaves of the expectation carry a claim (they differ from the run) and which
merely reproduce it. It is a comparison of the two values, not a mutation, so
it stays exact where a mutation would only be indicative.

Nothing here executes anything: a mutant is a value, the comparison is
canonical JSON equality, and running a variant remains the caller's business.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TypeAlias, cast

from stepfork.diff.compare import values_equal
from stepfork.export.scoring import MutantOutcome
from stepfork.trace import JsonValue, RunEnd, RunStatus, Trace

RECORDED_RUN_MUTANT = "the recorded run"
"""Name of the claim-drawn mutant: the run's own output, unscored for shape."""

LeafPath: TypeAlias = tuple[str | int, ...]
"""A leaf's location, as a walk from the root: keys as strings, indices as ints."""

PathValue: TypeAlias = tuple[str, JsonValue]
"""A mutant label and the value it substitutes at one leaf."""


def recorded_output(trace: Trace) -> JsonValue | None:
    """Return the output of the last completed run, or None when it has none.

    A trace that ended in a failure state carries no output, and neither does
    one recorded before the run reached ``RunEnd``. The absence is reported
    rather than worked around: with no recorded output there is nothing to
    draw a claim-drawn mutant from, and the honest verdict is that the battery
    cannot separate the two readings.
    """
    for event in reversed(trace.events):
        if isinstance(event, RunEnd) and event.run_status is RunStatus.COMPLETED:
            return event.output
    return None


def render_path(path: LeafPath) -> str:
    """Render a leaf path the way :func:`first_difference` renders one."""
    rendered = ""
    for step in path:
        if isinstance(step, int):
            rendered += f"[{step}]"
        else:
            rendered += f".{step}" if rendered else step
    return rendered or "$"


@dataclass(frozen=True)
class PinCoverage:
    """Which leaves of an expectation carry a claim, and which reproduce the run.

    ``reproduced`` leaves are not defects: a fix does not change every field,
    and an expectation that requires an untouched field to keep its recorded
    value is asserting the fix, not the run. What they are is *not evidence of
    anchoring*, and a summary that reported only a count of leaves would read
    them as though they were.
    """

    live: tuple[str, ...]
    """Paths whose expectation value differs from the recorded run's value."""

    reproduced: tuple[str, ...]
    """Paths whose expectation value equals the recorded run's value."""

    @property
    def total(self) -> int:
        """Number of leaves compared."""
        return len(self.live) + len(self.reproduced)

    @property
    def anchored(self) -> bool:
        """True when at least one leaf carries a value the run did not produce."""
        return bool(self.live)


def derive_battery(
    expectation: JsonValue,
    recorded: JsonValue | None,
    *,
    has_expectation: bool = True,
) -> tuple[MutantOutcome, ...]:
    """Derive a mutant battery from an expectation and a recorded run.

    The battery is deterministic: leaves are visited in sorted key order, and
    each leaf contributes its perturbations in a fixed order, so two runs over
    the same inputs produce the same rows with the same names.

    Args:
        expectation: The expectation the generated test asserts.
        recorded: The recorded run's output, or None when the trace has none.
            None is not an error: the battery is then the code-drawn controls
            alone, which scores as ``undetermined`` -- the honest reading of a
            battery that cannot separate the two.
        has_expectation: False when the export carried no expectation at all.
            The battery is then empty, because the generated test performs no
            comparison for a mutant to change the outcome of.

    Returns:
        One claim-drawn row (when a recorded output exists) followed by the
        code-drawn control rows.
    """
    if not has_expectation:
        return ()

    mutants: list[MutantOutcome] = []
    if recorded is not None:
        mutants.append(
            MutantOutcome(
                name=RECORDED_RUN_MUTANT,
                kind="claim",
                output=recorded,
            )
        )

    for path, value in _leaves(expectation):
        found, recorded_value = (
            _at(recorded, path) if recorded is not None else (False, None)
        )
        for label, replacement in _perturbations(value):
            if found and values_equal(replacement, recorded_value):
                continue
            mutants.append(
                MutantOutcome(
                    name=f"{render_path(path)}: {label}",
                    kind="code",
                    output=_replace(expectation, path, replacement),
                )
            )
    return tuple(mutants)


def pin_coverage(expectation: JsonValue, recorded: JsonValue) -> PinCoverage:
    """Report which expectation leaves a recorded run can and cannot account for.

    A leaf the run does not carry at all counts as live: the claim states
    something about a value the run never produced, which is a claim the run
    cannot have pinned.
    """
    live: list[str] = []
    reproduced: list[str] = []
    for path, value in _leaves(expectation):
        found, recorded_value = _at(recorded, path)
        if found and values_equal(value, recorded_value):
            reproduced.append(render_path(path))
        else:
            live.append(render_path(path))
    return PinCoverage(live=tuple(live), reproduced=tuple(reproduced))


def _is_leaf(value: JsonValue) -> bool:
    """True for a scalar, and for a container with nothing inside it.

    An empty container is a leaf because there is no value below it to compare:
    ``{"a": {}}`` claims that ``a`` holds nothing, and a run that filled ``a``
    contradicts that claim at ``a`` itself.
    """
    if isinstance(value, (dict, list)):
        return not value
    return True


def _leaves(
    value: JsonValue, path: LeafPath = ()
) -> Iterator[tuple[LeafPath, JsonValue]]:
    """Yield every leaf of a JSON value, in sorted key order."""
    if _is_leaf(value):
        yield path, value
        return
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _leaves(value[key], (*path, key))
        return
    items = cast("list[JsonValue]", value)
    for index, item in enumerate(items):
        yield from _leaves(item, (*path, index))


def _at(value: JsonValue | None, path: LeafPath) -> tuple[bool, JsonValue]:
    """Return whether a value sits at a path, and the value if one does."""
    current: JsonValue | None = value
    for step in path:
        if isinstance(step, int):
            if not isinstance(current, list) or step >= len(current):
                return False, None
            current = cast("list[JsonValue]", current)[step]
            continue
        if not isinstance(current, dict) or step not in current:
            return False, None
        current = cast("dict[str, JsonValue]", current)[step]
    return True, current


def _replace(value: JsonValue, path: LeafPath, replacement: JsonValue) -> JsonValue:
    """Return a copy of a value with one leaf replaced."""
    if not path:
        return replacement
    step, *rest = path
    tail: LeafPath = tuple(rest)
    if isinstance(step, int):
        items = cast("list[JsonValue]", value)
        return [
            _replace(item, tail, replacement) if index == step else item
            for index, item in enumerate(items)
        ]
    mapping = cast("dict[str, JsonValue]", value)
    return {
        key: _replace(item, tail, replacement) if key == step else item
        for key, item in mapping.items()
    }


def _perturbations(value: JsonValue) -> tuple[PathValue, ...]:
    """Return the code-drawn perturbations available for one leaf value.

    Only plausible, shape-preserving variants are offered: a value that could
    not have come out of a working implementation is not a mutant, it is noise.
    ``bool`` is checked before ``int`` because ``bool`` is a subclass of it.

    A perturbation that would leave the value unchanged is dropped. Floating
    point makes that reachable -- ``1e308 + 1.0`` is ``1e308`` -- and a row
    that carries the claim's own value is not a mutant: it would survive every
    expectation, including the ones the battery exists to convict.
    """
    if isinstance(value, bool):
        candidates: tuple[PathValue, ...] = (
            ("flipped", not value),
            ("as a string", "True" if value else "False"),
        )
    elif isinstance(value, int):
        candidates = (("off by one", value + 1), ("as a string", str(value)))
    elif isinstance(value, float):
        candidates = (("off by one", value + 1.0), ("as a string", repr(value)))
    elif isinstance(value, str):
        candidates = (("with a suffix", f"{value}?"), ("as null", None))
    elif value is None:
        candidates = (("as zero", 0), ("as an empty string", ""))
    else:
        return ()
    return tuple(
        candidate for candidate in candidates if not values_equal(candidate[1], value)
    )
