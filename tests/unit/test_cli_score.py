from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from stepfork import record
from stepfork.cli.main import app
from stepfork.export.generator import generate_pytest_source
from stepfork.trace import JsonValue

runner = CliRunner()

RECORDED: JsonValue = {"status": "refunded", "amount": 0.0}
CLAIM: JsonValue = {"status": "refunded", "amount": 42.5}


def _flat(text: str) -> str:
    """Collapse the line breaks the console inserts when it wraps."""
    return re.sub(r"\s+", " ", text)


def _record(bundle: Path, output: JsonValue) -> Path:
    with record("refund-agent", output=bundle) as session:
        session.set_output(output)
    return bundle


def _write_case(
    tmp_path: Path,
    *,
    recorded: JsonValue = RECORDED,
    expectation: JsonValue = CLAIM,
    has_expectation: bool = True,
    record_run: bool = True,
) -> tuple[Path, Path]:
    bundle = tmp_path / "refund.sftrace"
    if record_run:
        _record(bundle, recorded)
    test = tmp_path / "test_refund_regression.py"
    test.write_text(
        generate_pytest_source(
            trace_path=bundle,
            output_path=test,
            entrypoint="pkg.mod:run",
            expectation=expectation,
            has_expectation=has_expectation,
        ),
        encoding="utf-8",
    )
    return bundle, test


def test_score_reports_a_claim_pinned_expectation(tmp_path: Path) -> None:
    _, test = _write_case(tmp_path)
    result = runner.invoke(app, ["score", str(test)])
    assert result.exit_code == 0
    assert "claim-pinned" in _flat(result.stdout)
    assert "the recorded run" in _flat(result.stdout)


def test_score_convicts_a_run_pinned_expectation(tmp_path: Path) -> None:
    _, test = _write_case(tmp_path, expectation=RECORDED)
    result = runner.invoke(app, ["score", str(test)])
    assert result.exit_code == 1
    assert "run-pinned" in _flat(result.stdout)
    assert "unapproved oracle" in _flat(result.stdout)


def test_score_prints_json(tmp_path: Path) -> None:
    _, test = _write_case(tmp_path)
    result = runner.invoke(app, ["score", str(test), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "claim_pinned"
    assert payload["pins"] == {"live": ["amount"], "reproduced": ["status"]}
    assert payload["total"] == payload["killed"]
    assert {mutant["kind"] for mutant in payload["mutants"]} == {"claim", "code"}


def test_score_accepts_an_expectation_override(tmp_path: Path) -> None:
    """Scoring a candidate expectation is the pre-export review step."""
    _, test = _write_case(tmp_path, expectation=RECORDED)
    candidate = tmp_path / "expected.json"
    candidate.write_text(json.dumps(CLAIM), encoding="utf-8")
    assert runner.invoke(app, ["score", str(test)]).exit_code == 1
    result = runner.invoke(app, ["score", str(test), "--expect-output", str(candidate)])
    assert result.exit_code == 0
    assert "claim-pinned" in _flat(result.stdout)


def test_score_reports_a_missing_bundle(tmp_path: Path) -> None:
    _, test = _write_case(tmp_path, record_run=False)
    result = runner.invoke(app, ["score", str(test)])
    assert result.exit_code == 2
    assert "trace bundle not found" in _flat(result.stdout)


def test_score_names_a_bundle_it_was_given(tmp_path: Path) -> None:
    bundle, test = _write_case(tmp_path, expectation=RECORDED)
    test.write_text(
        test.read_text(encoding="utf-8").replace("TRACE_PATH = ", "TRACE_PATH_OLD = "),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["score", str(test), "--bundle", str(bundle)])
    assert result.exit_code == 1
    assert "run-pinned" in _flat(result.stdout)


def test_score_without_a_bundle_asks_for_one(tmp_path: Path) -> None:
    _, test = _write_case(tmp_path, record_run=False)
    test.write_text(
        test.read_text(encoding="utf-8").replace("TRACE_PATH = ", "TRACE_PATH_OLD = "),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["score", str(test)])
    assert result.exit_code == 2
    assert "--bundle" in _flat(result.stdout)


def test_score_reports_an_export_without_an_expectation(tmp_path: Path) -> None:
    _, test = _write_case(tmp_path, has_expectation=False, expectation=None)
    result = runner.invoke(app, ["score", str(test)])
    assert result.exit_code == 2
    assert "no expectation to score" in _flat(result.stdout)


def test_score_cannot_separate_readings_without_a_recorded_run(tmp_path: Path) -> None:
    """A run that recorded no output leaves nothing claim-drawn to derive."""
    _, test = _write_case(tmp_path, recorded=None)
    result = runner.invoke(app, ["score", str(test)])
    assert result.exit_code == 2
    assert "undetermined" in _flat(result.stdout)


def test_score_reports_an_unreadable_test_file(tmp_path: Path) -> None:
    test = tmp_path / "test_handwritten.py"
    test.write_text("EXPECTATION = build_expectation()\n", encoding="utf-8")
    result = runner.invoke(app, ["score", str(test)])
    assert result.exit_code == 2
    assert "not a literal value" in _flat(result.stdout)


def test_score_is_registered() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "score" in _flat(result.stdout)
