from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from stepfork import record
from stepfork.cli.main import app
from stepfork.export.generator import generate_pytest_source
from stepfork.export.receipt import (
    EXIT_COULD_NOT_EXECUTE,
    EXIT_FAILED,
    EXIT_PASSED,
    VERDICT_COULD_NOT_EXECUTE,
    VERDICT_FAILED,
    VERDICT_PASSED,
    bundle_digest,
)
from stepfork.trace import JsonValue

runner = CliRunner()

RECORDED: JsonValue = {"status": "refunded", "amount": 42.5}
CLAIM: JsonValue = {"status": "refunded", "amount": 42.5}


def _flat(text: str) -> str:
    """Collapse the line breaks the console inserts when it wraps."""
    return re.sub(r"\s+", " ", text)


def _record(bundle: Path, output: JsonValue) -> Path:
    with record("refund-agent", output=bundle) as session:
        session.set_output(output)
    return bundle


def _export(tmp_path: Path) -> tuple[Path, Path]:
    """Record a bundle and export the regression test that names it."""
    bundle = _record(tmp_path / "refund.sftrace", RECORDED)
    test = tmp_path / "test_refund_regression.py"
    test.write_text(
        generate_pytest_source(
            trace_path=bundle,
            output_path=test,
            entrypoint="pkg.mod:run",
            expectation=CLAIM,
        ),
        encoding="utf-8",
    )
    return bundle, test


def _plain_test(tmp_path: Path, body: str) -> Path:
    """A test file that exercises the runner without needing a real bundle."""
    test = tmp_path / "test_plain.py"
    test.write_text(body, encoding="utf-8")
    return test


def test_run_reports_a_missing_bundle_as_could_not_execute(tmp_path: Path) -> None:
    """The headline case: pytest alone reports this as a caught regression."""
    bundle, test = _export(tmp_path)
    bundle.rename(tmp_path / "refund.moved")

    receipt = tmp_path / "receipt.json"
    result = runner.invoke(app, ["run", str(test), "--receipt", str(receipt)])

    assert result.exit_code == EXIT_COULD_NOT_EXECUTE
    assert "COULD NOT EXECUTE" in _flat(result.stdout)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["verdict"] == VERDICT_COULD_NOT_EXECUTE
    assert payload["reason"] == "bundle_missing"
    # pytest was never started, so no failure could be manufactured for it.
    assert payload["exit_status"] is None
    assert payload["counts"]["failures"] == 0


def test_run_reports_a_passing_check(tmp_path: Path) -> None:
    test = _plain_test(tmp_path, "def test_ok():\n    assert 1 + 1 == 2\n")
    receipt = tmp_path / "receipt.json"

    result = runner.invoke(app, ["run", str(test), "--receipt", str(receipt)])

    assert result.exit_code == EXIT_PASSED
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["verdict"] == VERDICT_PASSED
    assert payload["reason"] is None
    assert payload["exit_status"] == 0
    assert payload["counts"] == {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}


def test_run_reports_a_failing_check(tmp_path: Path) -> None:
    test = _plain_test(tmp_path, "def test_bad():\n    assert 1 + 1 == 3\n")
    receipt = tmp_path / "receipt.json"

    result = runner.invoke(app, ["run", str(test), "--receipt", str(receipt)])

    assert result.exit_code == EXIT_FAILED
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["verdict"] == VERDICT_FAILED
    assert payload["counts"]["failures"] == 1


def test_run_separates_a_collection_error_from_a_failure(tmp_path: Path) -> None:
    """A module the check cannot import is not a statement about the agent."""
    test = _plain_test(
        tmp_path,
        "import absent_module_for_the_receipt_test  # noqa: F401\n\n"
        "def test_never_runs():\n    assert True\n",
    )
    receipt = tmp_path / "receipt.json"

    result = runner.invoke(app, ["run", str(test), "--receipt", str(receipt)])

    assert result.exit_code == EXIT_COULD_NOT_EXECUTE
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["verdict"] == VERDICT_COULD_NOT_EXECUTE
    assert payload["reason"] == "check_error"
    assert payload["counts"]["errors"] >= 1


def test_run_reports_an_empty_file_as_no_tests_collected(tmp_path: Path) -> None:
    test = _plain_test(tmp_path, "# no tests here\n")
    receipt = tmp_path / "receipt.json"

    result = runner.invoke(app, ["run", str(test), "--receipt", str(receipt)])

    assert result.exit_code == EXIT_COULD_NOT_EXECUTE
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["reason"] == "no_tests_collected"


def test_receipt_names_the_test_and_bundle_it_rests_on(tmp_path: Path) -> None:
    _, test = _export(tmp_path)
    receipt = tmp_path / "nested" / "receipt.json"

    result = runner.invoke(app, ["run", str(test), "--receipt", str(receipt)])

    payload = json.loads(receipt.read_text(encoding="utf-8"))
    # a real bundle is named, and the run got far enough to have a verdict
    assert payload["bundle"] is not None
    assert payload["test_sha256"]
    assert payload["test"].endswith("test_refund_regression.py")
    assert payload["receipt_version"] == 1
    assert payload["stepfork_version"]
    assert payload["created_at"].endswith("Z")
    # The exported test cannot import pkg.mod in this environment, so its body
    # raises. pytest reports an exception raised inside a test body as
    # `failure`, so this verdict is FAILED -- the documented limit of the
    # approach: the runner can pre-check the artifacts it can name (the
    # bundle), not the imports the test body performs. The test asserts the
    # limit rather than papering over it.
    assert payload["verdict"] == VERDICT_FAILED
    assert result.exit_code == EXIT_FAILED


def test_bundle_digest_changes_with_the_bundle_content(tmp_path: Path) -> None:
    bundle = _record(tmp_path / "refund.sftrace", RECORDED)
    first = bundle_digest(bundle)

    # rewrite one file inside the bundle and re-digest
    target = sorted(path for path in bundle.rglob("*") if path.is_file())[0]
    target.write_bytes(target.read_bytes() + b"\n")

    assert bundle_digest(bundle) != first


def test_run_prints_the_receipt_as_json(tmp_path: Path) -> None:
    test = _plain_test(tmp_path, "def test_ok():\n    assert True\n")

    result = runner.invoke(app, ["run", str(test), "--json"])

    assert result.exit_code == EXIT_PASSED
    payload = json.loads(result.stdout)
    assert payload["verdict"] == VERDICT_PASSED


def test_run_accepts_an_explicit_bundle(tmp_path: Path) -> None:
    bundle, test = _export(tmp_path)
    receipt = tmp_path / "receipt.json"

    result = runner.invoke(
        app, ["run", str(test), "--bundle", str(bundle), "--receipt", str(receipt)]
    )

    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["bundle"] == str(bundle)
    assert payload["bundle_digest"]
    # the bundle resolves and digests; whether the check then executes is up to
    # the entrypoint it names, which this fixture does not ship
    assert result.exit_code == EXIT_FAILED
