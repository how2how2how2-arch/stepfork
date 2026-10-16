from __future__ import annotations

from collections.abc import Sequence

from hypothesis import given
from hypothesis import strategies as st

from stepfork.diff.compare import first_difference
from stepfork.export.mutants import (
    RECORDED_RUN_MUTANT,
    PinCoverage,
    _perturbations,
    derive_battery,
    pin_coverage,
    recorded_output,
    render_path,
)
from stepfork.export.scoring import MutantKind, MutantOutcome, score_expectation
from stepfork.trace import JsonValue, RunEnd, RunStart, RunStatus, Trace

# A recorded refund run: the agent reported the refund as done and issued
# nothing. The claim is that a refund of 42.50 reaches the order.
RECORDED_RUN: JsonValue = {"status": "refunded", "amount": 0.0}
CLAIM_PINNED: JsonValue = {"status": "refunded", "amount": 42.5}
RUN_PINNED: JsonValue = {"status": "refunded", "amount": 0.0}


def _derived(
    expectation: JsonValue, recorded: JsonValue | None
) -> tuple[MutantOutcome, ...]:
    return derive_battery(expectation, recorded)


def _names(battery: Sequence[MutantOutcome]) -> list[str]:
    return [mutant.name for mutant in battery]


def _by_kind(battery: Sequence[MutantOutcome], kind: MutantKind) -> list[MutantOutcome]:
    return [mutant for mutant in battery if mutant.kind == kind]


def test_derived_battery_separates_a_claim_pinned_expectation() -> None:
    score = score_expectation(CLAIM_PINNED, _derived(CLAIM_PINNED, RECORDED_RUN))
    assert score.verdict == "claim_pinned"
    assert score.total > 1


def test_derived_battery_convicts_a_run_pinned_expectation() -> None:
    """The finding the derivation exists to produce: the claim row survives."""
    score = score_expectation(RUN_PINNED, _derived(RUN_PINNED, RECORDED_RUN))
    assert score.verdict == "run_pinned"
    survivors = [mutant.name for mutant in score.survivors]
    assert survivors == [RECORDED_RUN_MUTANT]
    # The code-drawn controls are killed either way, which is why the reading
    # cannot come from them.
    assert all(mutant.killed for mutant in score.mutants if mutant.kind == "code")


def test_claim_mutant_is_the_recorded_run_whole_not_a_projection() -> None:
    """A projection would hide an expectation that is stricter than the run."""
    recorded: JsonValue = {"status": "refunded", "amount": 0.0, "receipt": "r-1"}
    claim: JsonValue = {"status": "refunded"}
    battery = _derived(claim, recorded)
    claim_rows = _by_kind(battery, "claim")
    assert len(claim_rows) == 1
    assert claim_rows[0].output == recorded
    score = score_expectation(claim, battery)
    assert score.verdict == "claim_pinned"


def test_expectation_stricter_than_the_run_is_never_run_pinned() -> None:
    """`{}` claims an empty output; a run that produced fields contradicts it."""
    recorded: JsonValue = {"status": "refunded"}
    score = score_expectation({}, _derived({}, recorded))
    assert score.verdict == "claim_pinned"


def test_no_recorded_output_yields_controls_only() -> None:
    """Without a recorded run there is nothing claim-drawn to derive."""
    battery = _derived(CLAIM_PINNED, None)
    assert _by_kind(battery, "claim") == []
    assert _by_kind(battery, "code") != []
    assert score_expectation(CLAIM_PINNED, battery).verdict == "undetermined"


def test_no_expectation_yields_an_empty_battery() -> None:
    assert derive_battery(None, RECORDED_RUN, has_expectation=False) == ()
    score = score_expectation(None, (), has_expectation=False)
    assert score.verdict == "no_expectation"


def test_a_perturbation_that_lands_on_the_recorded_value_is_skipped() -> None:
    """A code-drawn label on the run's own value would be a claim row in disguise."""
    claim: JsonValue = {"amount": 42.5}
    recorded: JsonValue = {"amount": 43.5}  # the "off by one" perturbation
    battery = _derived(claim, recorded)
    names = _names(battery)
    assert "amount: off by one" not in names
    assert "amount: as a string" in names
    # Only the perturbation that collides is dropped; the claim row is not.
    assert _names(_by_kind(battery, "claim")) == [RECORDED_RUN_MUTANT]


def test_code_drawn_rows_carry_the_claim_with_one_leaf_changed() -> None:
    battery = _by_kind(_derived(CLAIM_PINNED, RECORDED_RUN), "code")
    assert battery
    for mutant in battery:
        difference = first_difference(CLAIM_PINNED, mutant.output)
        assert difference is not None
    names = _names(_derived(CLAIM_PINNED, RECORDED_RUN))
    assert "amount: as a string" in names
    assert "status: with a suffix" in names


def test_leaf_paths_are_rendered_the_way_first_difference_renders_them() -> None:
    claim: JsonValue = {"plan": ["search", "book"], "meta": {"city": "Paris"}}
    recorded: JsonValue = {"plan": ["search", "cancel"], "meta": {"city": "Paris"}}
    names = _names(_derived(claim, recorded))
    assert "plan[1]: with a suffix" in names
    difference = first_difference(claim, recorded)
    assert difference is not None
    assert difference[0] == "plan[1]"
    assert render_path(("plan", 1)) == difference[0]
    assert render_path(()) == "$"


def test_battery_is_deterministic() -> None:
    claim: JsonValue = {"a": 1, "b": {"c": "x"}, "d": True}
    first = _derived(claim, {"a": 2, "b": {"c": "y"}, "d": False})
    second = _derived(claim, {"a": 2, "b": {"c": "y"}, "d": False})
    assert first == second


def test_pin_coverage_splits_live_leaves_from_reproduced_ones() -> None:
    claim: JsonValue = {"amount": 42.5, "status": "refunded"}
    coverage = pin_coverage(claim, RECORDED_RUN)
    assert isinstance(coverage, PinCoverage)
    assert coverage.live == ("amount",)
    assert coverage.reproduced == ("status",)
    assert coverage.total == 2
    assert coverage.anchored


def test_pin_coverage_counts_a_leaf_the_run_never_produced_as_live() -> None:
    coverage = pin_coverage({"amount": 42.5, "receipt": "r-1"}, {"amount": 42.5})
    assert coverage.live == ("receipt",)
    assert coverage.reproduced == ("amount",)


def test_an_empty_container_is_a_leaf_the_run_can_contradict() -> None:
    claim: JsonValue = {"trace": {}}
    recorded: JsonValue = {"trace": {"steps": 3}}
    coverage = pin_coverage(claim, recorded)
    assert coverage.live == ("trace",)
    assert score_expectation(claim, _derived(claim, recorded)).verdict == "claim_pinned"


def test_recorded_output_reads_the_last_completed_run() -> None:
    trace = Trace(agent_name="probe")
    trace.add(RunStart())
    trace.add(RunEnd(run_status=RunStatus.COMPLETED, output={"ok": True}))
    trace.add(RunEnd(run_status=RunStatus.FAILED))
    assert recorded_output(trace) == {"ok": True}


def test_recorded_output_is_none_when_no_run_completed() -> None:
    trace = Trace(agent_name="probe")
    trace.add(RunStart())
    trace.add(RunEnd(run_status=RunStatus.FAILED))
    assert recorded_output(trace) is None
    assert recorded_output(Trace(agent_name="empty")) is None


@given(
    st.one_of(
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(),
        st.none(),
    )
)
def test_every_perturbation_changes_its_value(value: JsonValue) -> None:
    """A no-op row would survive every expectation, convicting nothing."""
    for label, replacement in _perturbations(value):
        assert label
        assert first_difference(value, replacement) is not None
