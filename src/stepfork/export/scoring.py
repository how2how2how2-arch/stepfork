"""Score an exported expectation against a mutant battery.

An exported regression test asserts ``EXPECTATION``. Export fixes *what* is
asserted; it says nothing about whether that expectation is still anchored to
the claim, or has drifted into a copy of the run it was written from. The two
are indistinguishable in a green run, which is why the gap needs its own
instrument.

A battery is a set of mutants, and each mutant names an implementation variant
together with the JSON that variant produced. :func:`score_expectation` applies
the same canonical comparison the generated test uses and reports which mutants
the expectation rejects. It compares values only and never executes anything:
running a variant stays with the caller, so a battery can also be built from
recorded results.

Mutants come in two kinds, and the distinction is the whole point:

``"code"``
    A perturbation drawn from the implementation: an operator flipped, a
    constant changed. Both a claim-pinned and a run-pinned expectation reject
    these, so a battery made only of them cannot separate the two.

``"claim"``
    A variant drawn from the requirement: it violates the claim while staying
    plausible. A claim-pinned expectation rejects it. A run-pinned expectation,
    which restates what the code did, accepts it.

The verdict therefore reads off the claim-drawn mutants alone:

* ``claim_pinned`` -- every claim-drawn mutant is rejected.
* ``run_pinned`` -- a claim-drawn mutant survives, so the expectation does not
  encode the claim. Treat it as an unapproved oracle before trusting it.
* ``undetermined`` -- the battery carries no claim-drawn mutant, so it cannot
  tell the two apart. This is the honest reading of a code-only battery, and
  the reason it is a verdict rather than a pass.
* ``no_expectation`` -- the export carried no expectation, so the test asserts
  the recorded run and there is nothing to score.

Measured on the shape this module exists for: three code-drawn mutants score a
claim-pinned and a run-pinned expectation identically, 3/3 and 3/3. Adding one
claim-drawn mutant separates them, 4/4 against 3/4.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from stepfork.diff.compare import first_difference
from stepfork.trace.models import JsonValue

MutantKind = Literal["code", "claim"]
Verdict = Literal["claim_pinned", "run_pinned", "undetermined", "no_expectation"]

_KINDS: frozenset[str] = frozenset({"code", "claim"})


class ScoringError(ValueError):
    """Raised when a mutant battery cannot be scored."""


@dataclass(frozen=True)
class MutantOutcome:
    """One implementation variant and the JSON it produced.

    ``output`` is the value the variant returned, which the expectation is
    compared against. Build one from a recorded result when the variant cannot
    be run in the scoring process.
    """

    name: str
    kind: MutantKind
    output: JsonValue


@dataclass(frozen=True)
class MutantScore:
    """Whether the expectation rejected one mutant."""

    name: str
    kind: MutantKind
    killed: bool
    differing_path: str | None = None
    """Path of the first differing leaf, when the expectation rejected it."""

    @property
    def survived(self) -> bool:
        """True when the expectation accepted the mutant."""
        return not self.killed


@dataclass(frozen=True)
class ExpectationScore:
    """The result of scoring one expectation against one battery."""

    verdict: Verdict
    mutants: tuple[MutantScore, ...]

    @property
    def total(self) -> int:
        """Number of mutants scored."""
        return len(self.mutants)

    @property
    def killed(self) -> int:
        """Number of mutants the expectation rejected."""
        return sum(1 for mutant in self.mutants if mutant.killed)

    @property
    def survivors(self) -> tuple[MutantScore, ...]:
        """Mutants the expectation accepted."""
        return tuple(mutant for mutant in self.mutants if mutant.survived)

    @property
    def discrimination(self) -> float:
        """Fraction of the battery the expectation rejected, from 0.0 to 1.0."""
        if not self.mutants:
            return 0.0
        return self.killed / self.total

    def by_kind(self, kind: MutantKind) -> tuple[MutantScore, ...]:
        """Return the scored mutants drawn from one source."""
        return tuple(mutant for mutant in self.mutants if mutant.kind == kind)

    def summary(self) -> str:
        """Return a one-line reading of the score."""
        if self.verdict == "no_expectation":
            return "no expectation to score: the test asserts the recorded run"
        if self.verdict == "undetermined":
            return (
                f"rejected {self.killed}/{self.total} mutants, but the battery "
                "carries no claim-drawn mutant, so it cannot separate a "
                "claim-pinned expectation from a run-pinned one"
            )
        if self.verdict == "run_pinned":
            survived = ", ".join(
                mutant.name for mutant in self.by_kind("claim") if mutant.survived
            )
            return (
                "expectation is run-pinned: it accepts the claim-drawn "
                f"mutant(s) {survived}, so it encodes the recorded run rather "
                "than the claim"
            )
        return (
            f"expectation is claim-pinned: rejected {self.killed}/{self.total} "
            "mutants, including every claim-drawn mutant"
        )


def score_expectation(
    expectation: JsonValue | None,
    mutants: Iterable[MutantOutcome],
    *,
    has_expectation: bool = True,
) -> ExpectationScore:
    """Score an exported expectation against a mutant battery.

    Each mutant is scored with the same canonical comparison the generated
    regression test uses, so a mutant the expectation rejects is one the test
    would have failed against.

    A battery must contain at least one claim-drawn mutant for the verdict to
    separate the two readings; without one the result is ``undetermined``
    rather than a pass, because a code-only battery scores a drifted
    expectation exactly as it scores an anchored one.

    Args:
        expectation: The exported expectation, as embedded in the generated
            test.
        mutants: The battery to score against.
        has_expectation: False when the export carried no expectation. The
            battery is then not scored, because the generated test performs no
            comparison.

    Returns:
        The verdict, the per-mutant scores, and their counts.

    Raises:
        ScoringError: If a mutant name is empty or repeated, or if a mutant
            carries a kind other than ``"code"`` or ``"claim"``.
    """
    battery = _validate(mutants)
    if not has_expectation:
        return ExpectationScore(verdict="no_expectation", mutants=())
    scored = tuple(_score_one(expectation, mutant) for mutant in battery)
    return ExpectationScore(verdict=_verdict(scored), mutants=scored)


def _validate(mutants: Iterable[MutantOutcome]) -> tuple[MutantOutcome, ...]:
    battery = tuple(mutants)
    seen: set[str] = set()
    for mutant in battery:
        if not mutant.name.strip():
            raise ScoringError("mutant names must be non-empty")
        if mutant.name in seen:
            raise ScoringError(f"duplicate mutant name: {mutant.name!r}")
        seen.add(mutant.name)
        if mutant.kind not in _KINDS:
            raise ScoringError(
                f"unknown mutant kind {mutant.kind!r} for {mutant.name!r}: "
                "expected 'code' or 'claim'"
            )
    return battery


def _score_one(expectation: JsonValue | None, mutant: MutantOutcome) -> MutantScore:
    difference = first_difference(expectation, mutant.output)
    if difference is None:
        return MutantScore(name=mutant.name, kind=mutant.kind, killed=False)
    path, _, _ = difference
    return MutantScore(
        name=mutant.name,
        kind=mutant.kind,
        killed=True,
        differing_path=path,
    )


def _verdict(scored: tuple[MutantScore, ...]) -> Verdict:
    claim = [mutant for mutant in scored if mutant.kind == "claim"]
    if not claim:
        return "undetermined"
    if any(mutant.survived for mutant in claim):
        return "run_pinned"
    return "claim_pinned"
