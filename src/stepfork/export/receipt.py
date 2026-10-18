"""Runner-owned receipts for an exported regression test.

``stepfork export`` writes a pytest file whose body calls
:func:`stepfork.export.runtime.run_regression_case`. That file is the *check*.
This module is the *runner*: it decides whether the check executed at all, runs
it as a subprocess, and writes a receipt describing what happened.

The distinction matters because a pytest exit status cannot carry it. Measured
on pytest 9.1.1, with the test body raising rather than asserting:

===========================  ==========  ============
case                         exit status  JUnit kind
===========================  ==========  ============
assertion failed             1            ``failure``
the trace bundle is missing   1            ``failure``
a dependency is absent        1            ``failure``
a fixture cannot be built     1            ``error``
collection failed             2            ``error``
no tests were collected       5            (none)
===========================  ==========  ============

So on the artifact ``stepfork export`` actually produces, "the agent's
behaviour changed" and "the bundle this test names is not there" arrive as the
same status *and* the same JUnit kind. A CI that keys on either one reports a
missing artifact as a caught regression.

Two things fix that, and both belong to the runner rather than to the test:

1. **The artifacts are resolved before the check is launched.** If the bundle
   the test names is absent or unreadable, the receipt says
   ``could_not_execute`` and pytest is never started, so no failure can be
   manufactured for it.
2. **The receipt is written by this process, after the child exits, from what
   the child produced.** It records the verdict, the raw exit status, the
   JUnit counts, a digest of the bundle and a hash of the test file, so the
   three inputs the verdict rests on are named in the artifact itself.

The test under measurement never writes the receipt. A file the check writes
about itself is a file the check can get wrong, and the interesting failures
here are exactly the ones where the check does not run at all.

**What this does not catch.** The runner pre-checks the artifacts it can name
from the test's literals -- the bundle. It does not pre-check what the test
body *imports*: an entrypoint the local tree cannot resolve, or a dependency
imported inside the test, raises inside the test body, and pytest reports an
exception raised there as ``failure``, exactly as it reports a changed return
value. Those cases still read as ``failed``. Moving the check is the fix, not
widening the pattern match: resolving ``ENTRYPOINT`` before the launch, the way
the bundle is resolved, would turn them into ``could_not_execute`` too.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stepfork import __version__
from stepfork.export.testfile import ExportedTestError, read_exported_test

VERDICT_PASSED = "passed"
VERDICT_FAILED = "failed"
VERDICT_COULD_NOT_EXECUTE = "could_not_execute"

#: Exit codes `stepfork run` returns, so a CI can key on the status alone.
EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_COULD_NOT_EXECUTE = 2

EXIT_BY_VERDICT = {
    VERDICT_PASSED: EXIT_PASSED,
    VERDICT_FAILED: EXIT_FAILED,
    VERDICT_COULD_NOT_EXECUTE: EXIT_COULD_NOT_EXECUTE,
}

RECEIPT_VERSION = 1


@dataclass(frozen=True)
class ReceiptCounts:
    """The JUnit totals the verdict was derived from."""

    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0

    def to_json(self) -> dict[str, int]:
        return {
            "tests": self.tests,
            "failures": self.failures,
            "errors": self.errors,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class Receipt:
    """What the runner observed, written by the runner.

    ``reason`` is set whenever the verdict is ``could_not_execute``, and names
    the specific way the check failed to run: ``bundle_missing``,
    ``bundle_digest_failed``, ``check_error``, ``no_tests_collected``.
    """

    verdict: str
    test: str
    test_sha256: str
    bundle: str | None = None
    bundle_digest: str | None = None
    reason: str | None = None
    detail: str | None = None
    exit_status: int | None = None
    counts: ReceiptCounts = field(default_factory=ReceiptCounts)
    command: list[str] = field(default_factory=list)
    duration_ms: int = 0
    created_at: str = ""
    stepfork_version: str = __version__
    receipt_version: int = RECEIPT_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "receipt_version": self.receipt_version,
            "verdict": self.verdict,
            "reason": self.reason,
            "detail": self.detail,
            "exit_status": self.exit_status,
            "bundle": self.bundle,
            "bundle_digest": self.bundle_digest,
            "test": self.test,
            "test_sha256": self.test_sha256,
            "counts": self.counts.to_json(),
            "command": self.command,
            "duration_ms": self.duration_ms,
            "stepfork_version": self.stepfork_version,
            "created_at": self.created_at,
        }


def file_sha256(path: Path) -> str:
    """SHA-256 of one file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_digest(path: Path) -> str:
    """SHA-256 over a bundle's files, in sorted relative-path order.

    Hashing the file contents in a fixed order rather than the directory's
    metadata means the digest names the data, not the checkout: copying a
    bundle between machines leaves the digest unchanged, and editing any file
    in it changes it.
    """
    digest = hashlib.sha256()
    if path.is_dir():
        root = path
        entries = sorted(
            (candidate for candidate in path.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(root).as_posix(),
        )
    else:
        root = path.parent
        entries = [path]
    for candidate in entries:
        digest.update(candidate.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha256(candidate).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _counts_from_junit(path: Path) -> ReceiptCounts | None:
    """Read JUnit totals, or None when the report is absent or unreadable."""
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    if suite is None:
        return None

    def total(name: str) -> int:
        try:
            return int(suite.get(name) or 0)
        except ValueError:
            return 0

    return ReceiptCounts(
        tests=total("tests"),
        failures=total("failures"),
        errors=total("errors"),
        skipped=total("skipped"),
    )


def _first_error_message(path: Path) -> str | None:
    """The message of the first ``error`` element, trimmed for a receipt line."""
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    for case in root.iter("testcase"):
        for child in case:
            if child.tag != "error":
                continue
            message = (child.get("message") or "").strip().splitlines()
            if message:
                return message[0][:300]
    return None


def run_exported_test(
    test_path: Path,
    *,
    bundle: Path | None = None,
    pytest_args: list[str] | None = None,
    python: str = sys.executable,
    timeout_seconds: float | None = None,
    cwd: Path | None = None,
) -> Receipt:
    """Run one exported regression test and describe what happened.

    The bundle is resolved and digested *before* pytest starts, so a missing or
    unreadable bundle is reported as ``could_not_execute`` rather than as a
    failure the check itself cannot be distinguished from.
    """
    created = _now()
    pytest_args = list(pytest_args or [])

    try:
        test_sha = file_sha256(test_path)
    except OSError as exc:
        return Receipt(
            verdict=VERDICT_COULD_NOT_EXECUTE,
            reason="test_unreadable",
            detail=str(exc),
            test=str(test_path),
            test_sha256="",
            bundle=str(bundle) if bundle is not None else None,
            created_at=created,
        )

    resolved_bundle = bundle
    if resolved_bundle is None:
        try:
            resolved_bundle = read_exported_test(test_path).trace_path
        except ExportedTestError:
            # A hand-written test that is not an export names no bundle, so
            # there is no artifact to check first. That is not a refusal: the
            # verdict then rests on the JUnit counts alone, which still
            # separates "the check disagreed" from "the check never ran".
            resolved_bundle = None

    if resolved_bundle is not None and not resolved_bundle.exists():
        return Receipt(
            verdict=VERDICT_COULD_NOT_EXECUTE,
            reason="bundle_missing",
            detail=(
                f"the bundle this test names is not there: {resolved_bundle}; "
                "the check cannot run, so this is not a regression"
            ),
            test=str(test_path),
            test_sha256=test_sha,
            bundle=str(resolved_bundle),
            created_at=created,
        )

    digest: str | None = None
    try:
        if resolved_bundle is not None:
            digest = bundle_digest(resolved_bundle)
    except OSError as exc:
        return Receipt(
            verdict=VERDICT_COULD_NOT_EXECUTE,
            reason="bundle_unreadable",
            detail=str(exc),
            test=str(test_path),
            test_sha256=test_sha,
            bundle=str(resolved_bundle),
            created_at=created,
        )

    report = Path(tempfile.mkdtemp(prefix="stepfork-receipt-")) / "junit.xml"
    command = [
        python,
        "-m",
        "pytest",
        str(test_path),
        "-q",
        "--junit-xml",
        str(report),
        *pytest_args,
    ]

    started = time.monotonic()
    status: int | None = None
    detail: str | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        status = completed.returncode
    except subprocess.TimeoutExpired:
        detail = f"pytest did not finish within {timeout_seconds:g}s"
    except OSError as exc:
        detail = f"could not launch pytest: {exc}"
    duration_ms = int((time.monotonic() - started) * 1000)

    counts = _counts_from_junit(report)

    def receipt(verdict: str, reason: str | None, extra: str | None = None) -> Receipt:
        return Receipt(
            verdict=verdict,
            reason=reason,
            detail=extra if extra is not None else detail,
            exit_status=status,
            counts=counts or ReceiptCounts(),
            command=command,
            duration_ms=duration_ms,
            test=str(test_path),
            test_sha256=test_sha,
            bundle=str(resolved_bundle),
            bundle_digest=digest,
            created_at=created,
        )

    if detail is not None:
        return receipt(VERDICT_COULD_NOT_EXECUTE, "pytest_unavailable")

    if counts is None:
        return receipt(
            VERDICT_COULD_NOT_EXECUTE,
            "report_missing",
            "pytest produced no JUnit report, so the run cannot be classified",
        )

    if counts.errors:
        return receipt(
            VERDICT_COULD_NOT_EXECUTE,
            "check_error",
            _first_error_message(report)
            or f"{counts.errors} test(s) errored before their assertions ran",
        )

    if counts.tests == 0:
        return receipt(
            VERDICT_COULD_NOT_EXECUTE,
            "no_tests_collected",
            "pytest collected no tests from this file",
        )

    if status == 0 and not counts.failures:
        return receipt(VERDICT_PASSED, None)

    if counts.failures:
        return receipt(VERDICT_FAILED, None)

    # A non-zero status with no recorded failure or error is a pytest-level
    # problem (interrupted, internal error, bad arguments), not a verdict
    # about the agent.
    return receipt(
        VERDICT_COULD_NOT_EXECUTE,
        "pytest_error",
        f"pytest exited {status} without recording a test outcome",
    )


def write_receipt(receipt: Receipt, path: Path) -> None:
    """Write the receipt as JSON, creating the parent directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(receipt.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
