from __future__ import annotations

from typing import cast

import pytest

from stepfork.export.scoring import (
    ExpectationScore,
    MutantKind,
    MutantOutcome,
    ScoringError,
    score_expectation,
)

# A recorded refund run: the agent reported the refund as done and issued
# nothing. The claim is that a refund of 42.50 reaches the order.
RECORDED_RUN = {"status": "refunded", "amount": 0.0}
CLAIM_PINNED = {"status": "refunded", "amount": 42.50}
RUN_PINNED = dict(RECORDED_RUN)


def _code_battery() -> list[MutantOutcome]:
    """Mutants drawn from the implementation: an amount off by one, a
    misspelled status, and a stringified amount."""
    return [
        MutantOutcome(
            name="amount off by one",
            kind="code",
            output={"status": "refunded", "amount": 41.50},
        ),
        MutantOutcome(
            name="status misspelled",
            kind="code",
            output={"status": "refund", "amount": 42.50},
        ),
        MutantOutcome(
            name="amount as string",
            kind="code",
            output={"status": "refunded", "amount": "42.50"},
        ),
    ]


def _kills(score: ExpectationScore) -> set[str]:
    """Names of the mutants one expectation rejects."""
    return {mutant.name for mutant in score.mutants if mutant.killed}


def _claim_mutant() -> MutantOutcome:
    """The variant drawn from the claim: it violates the claim while staying
    plausible, and it is the one the recorded run came from."""
    return MutantOutcome(
        name="refund reported but not issued",
        kind="claim",
        output=dict(RECORDED_RUN),
    )


def test_code_only_battery_cannot_separate_the_two_expectations() -> None:
    anchored = score_expectation(CLAIM_PINNED, _code_battery())
    drifted = score_expectation(RUN_PINNED, _code_battery())

    assert (anchored.killed, anchored.total) == (3, 3)
    assert (drifted.killed, drifted.total) == (3, 3)
    # The two expectations agree on which mutants die. They may name a
    # different leaf inside a mutant, so the kill set is what is compared.
    assert _kills(anchored) == _kills(drifted) == {
        "amount off by one",
        "status misspelled",
        "amount as string",
    }
    assert anchored.verdict == "undetermined"
    assert drifted.verdict == "undetermined"


def test_claim_drawn_mutant_separates_them() -> None:
    battery = [*_code_battery(), _claim_mutant()]

    anchored = score_expectation(CLAIM_PINNED, battery)
    drifted = score_expectation(RUN_PINNED, battery)

    assert (anchored.killed, anchored.total) == (4, 4)
    assert (drifted.killed, drifted.total) == (3, 4)
    assert anchored.verdict == "claim_pinned"
    assert drifted.verdict == "run_pinned"


def test_run_pinned_expectation_accepts_the_claim_drawn_mutant() -> None:
    score = score_expectation(RUN_PINNED, [_claim_mutant()])

    survivors = score.survivors
    assert [mutant.name for mutant in survivors] == [
        "refund reported but not issued"
    ]
    assert survivors[0].survived
    assert survivors[0].differing_path is None
    assert "run-pinned" in score.summary()


def test_killed_mutant_reports_the_differing_path() -> None:
    score = score_expectation(CLAIM_PINNED, _code_battery())

    by_name = {mutant.name: mutant for mutant in score.mutants}
    assert by_name["amount off by one"].differing_path == "amount"
    assert by_name["status misspelled"].differing_path == "status"
    assert by_name["amount as string"].differing_path == "amount"
    assert all(mutant.killed for mutant in score.mutants)


def test_discrimination_and_counts() -> None:
    anchored = score_expectation(CLAIM_PINNED, [*_code_battery(), _claim_mutant()])
    drifted = score_expectation(RUN_PINNED, [*_code_battery(), _claim_mutant()])

    assert anchored.discrimination == 1.0
    assert drifted.discrimination == pytest.approx(0.75)
    assert len(anchored.by_kind("code")) == 3
    assert len(anchored.by_kind("claim")) == 1
    assert [mutant.name for mutant in drifted.survivors] == [
        "refund reported but not issued"
    ]


def test_battery_without_claim_mutants_is_undetermined_not_passed() -> None:
    score = score_expectation(CLAIM_PINNED, _code_battery())

    assert score.verdict == "undetermined"
    assert "cannot separate" in score.summary()


def test_empty_battery_is_undetermined() -> None:
    score = score_expectation(CLAIM_PINNED, [])

    assert score.verdict == "undetermined"
    assert score.total == 0
    assert score.discrimination == 0.0


def test_no_expectation_is_not_scored() -> None:
    score = score_expectation(
        None,
        [*_code_battery(), _claim_mutant()],
        has_expectation=False,
    )

    assert score.verdict == "no_expectation"
    assert score.mutants == ()
    assert score.total == 0
    assert "no expectation to score" in score.summary()


def test_null_expectation_is_scored_as_a_value() -> None:
    score = score_expectation(
        None,
        [MutantOutcome(name="returns null", kind="claim", output=None)],
    )

    assert score.verdict == "run_pinned"
    assert score.mutants[0].survived


def test_duplicate_mutant_names_are_rejected() -> None:
    battery = [
        MutantOutcome(name="same", kind="code", output={"a": 1}),
        MutantOutcome(name="same", kind="claim", output={"a": 2}),
    ]

    with pytest.raises(ScoringError, match="duplicate mutant name"):
        score_expectation({"a": 1}, battery)


def test_empty_mutant_name_is_rejected() -> None:
    with pytest.raises(ScoringError, match="non-empty"):
        score_expectation({"a": 1}, [MutantOutcome(name=" ", kind="code", output={})])


def test_unknown_mutant_kind_is_rejected() -> None:
    mutant = MutantOutcome(
        name="syntax mutant",
        kind=cast(MutantKind, "syntax"),
        output={"a": 1},
    )

    with pytest.raises(ScoringError, match="unknown mutant kind"):
        score_expectation({"a": 1}, [mutant])


def test_result_is_a_frozen_dataclass() -> None:
    score: ExpectationScore = score_expectation(CLAIM_PINNED, [_claim_mutant()])

    assert score.verdict == "claim_pinned"
    assert score.total == 1
    assert score.killed == 1
