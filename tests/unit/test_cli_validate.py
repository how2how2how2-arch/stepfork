from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer import rich_utils
from typer.testing import CliRunner

from stepfork import ToolCall, Trace
from stepfork.cli.main import app
from stepfork.export.receipt import bundle_digest
from stepfork.trace import RunEnd, RunStart, RunStatus, ToolResult
from stepfork.trace.integrity import IntegrityStatus, verify_bundle_integrity

runner = CliRunner()

INTEGRITY_COVERED = ("manifest.json", "events.jsonl", "redactions.json")


def complete_trace() -> Trace:
    trace = Trace(agent_name="demo-agent")
    start = trace.add(RunStart(input={"prompt": "book"}))
    call = trace.add(
        ToolCall(
            name="search",
            input={"query": "Kathmandu flights"},
            parent_id=start.id,
        )
    )
    result = trace.add(
        ToolResult(name="search", output={"flights": []}, parent_id=call.id)
    )
    trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=result.id))
    return trace


def test_validate_valid_trace(tmp_path: Path) -> None:
    path = complete_trace().save(
        tmp_path / "valid.sftrace",
        status=RunStatus.COMPLETED,
    )

    result = runner.invoke(app, ["validate", str(path)])

    assert result.exit_code == 0
    assert "Validation passed" in result.stdout
    assert "Mode" in result.stdout
    assert "strict" in result.stdout


def test_validate_with_integrity_verification(tmp_path: Path) -> None:
    path = complete_trace().save(
        tmp_path / "valid.sftrace",
        status=RunStatus.COMPLETED,
    )

    result = runner.invoke(app, ["validate", str(path), "--verify-integrity"])

    assert result.exit_code == 0
    assert "Integrity" in result.stdout
    assert "VERIFIED" in result.stdout


def test_validate_with_integrity_mismatch(tmp_path: Path) -> None:
    path = complete_trace().save(
        tmp_path / "tampered.sftrace",
        status=RunStatus.COMPLETED,
    )
    lines = (path / "events.jsonl").read_text().splitlines()
    event = json.loads(lines[1])
    event["input"]["query"] = "Pokhara flights"
    lines[1] = json.dumps(event)
    (path / "events.jsonl").write_text("\n".join(lines) + "\n")

    result = runner.invoke(app, ["validate", str(path), "--verify-integrity"])

    assert result.exit_code == 1
    assert "MISMATCH" in result.stdout
    assert "events.jsonl" in result.stdout


def test_validate_legacy_integrity_returns_unavailable(tmp_path: Path) -> None:
    path = complete_trace().save(
        tmp_path / "valid.sftrace",
        status=RunStatus.COMPLETED,
    )
    legacy = tmp_path / "legacy.sftrace"
    shutil.copytree(path, legacy)
    (legacy / "integrity.json").unlink()

    result = runner.invoke(app, ["validate", str(legacy), "--verify-integrity"])

    assert result.exit_code == 3
    assert "UNVERIFIED" in result.stdout


def test_validate_invalid_trace(tmp_path: Path) -> None:
    trace = complete_trace()
    trace.events[1] = trace.events[1].model_copy(update={"id": trace.events[0].id})
    path = trace.save(tmp_path / "invalid.sftrace", status=RunStatus.COMPLETED)

    result = runner.invoke(app, ["validate", str(path)])

    assert result.exit_code == 1
    assert "duplicate_event_id" in result.stdout
    assert "Validation failed" in result.stdout


def test_validate_partial_trace_with_partial_flag(tmp_path: Path) -> None:
    trace = Trace(agent_name="demo-agent")
    trace.add(ToolCall(name="search", input={"query": "Kathmandu flights"}))
    path = trace.save(tmp_path / "partial.sftrace")

    result = runner.invoke(app, ["validate", str(path), "--partial"])

    assert result.exit_code == 0
    assert "Validation passed" in result.stdout
    assert "partial" in result.stdout


def test_validate_missing_trace_returns_unreadable(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(tmp_path / "missing.sftrace")])

    assert result.exit_code == 2
    assert "missing_file" in result.stdout


def test_validate_help(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rich_utils, "MAX_WIDTH", 200)
    monkeypatch.setattr(rich_utils, "COLOR_SYSTEM", None)
    result = runner.invoke(app, ["validate", "--help"])

    assert result.exit_code == 0
    assert "--partial" in result.stdout
    assert "--expect-digest" in result.stdout
    assert ".sftrace" in result.stdout


def rewrite_integrity_record(path: Path) -> None:
    """Recompute ``integrity.json`` over the bundle's current payload bytes.

    This is what an edit looks like once the record is rewritten with it: the
    three payload files agree with ``integrity.json`` again, so the record on
    its own has nothing left to disagree with.
    """
    files = {
        name: hashlib.sha256((path / name).read_bytes()).hexdigest()
        for name in INTEGRITY_COVERED
    }
    record = {"schema_version": "0.1", "algorithm": "sha256", "files": files}
    (path / "integrity.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline=""
    )


def test_validate_expect_digest_matches(tmp_path: Path) -> None:
    path = complete_trace().save(
        tmp_path / "valid.sftrace",
        status=RunStatus.COMPLETED,
    )
    digest = bundle_digest(path)

    result = runner.invoke(app, ["validate", str(path), "--expect-digest", digest])

    assert result.exit_code == 0
    assert "MATCH" in result.stdout
    assert "Validation passed" in result.stdout


def test_validate_expect_digest_ignores_case_and_surrounding_space(
    tmp_path: Path,
) -> None:
    path = complete_trace().save(
        tmp_path / "valid.sftrace",
        status=RunStatus.COMPLETED,
    )
    digest = bundle_digest(path)

    result = runner.invoke(
        app, ["validate", str(path), "--expect-digest", f"  {digest.upper()}\n"]
    )

    assert result.exit_code == 0
    assert "MATCH" in result.stdout


def test_validate_expect_digest_catches_an_added_file(tmp_path: Path) -> None:
    path = complete_trace().save(
        tmp_path / "valid.sftrace",
        status=RunStatus.COMPLETED,
    )
    recorded = bundle_digest(path)
    (path / "agent-notes.txt").write_text("added after the receipt was written\n")

    # The integrity record does not cover the added file, which is exactly why
    # it cannot answer this question.
    assert verify_bundle_integrity(path).status is IntegrityStatus.VERIFIED

    result = runner.invoke(app, ["validate", str(path), "--expect-digest", recorded])

    assert result.exit_code == 1
    assert "NO MATCH" in result.stdout
    assert "Bundle digest check failed" in result.stdout


def test_validate_expect_digest_catches_a_rewritten_integrity_record(
    tmp_path: Path,
) -> None:
    path = complete_trace().save(
        tmp_path / "tampered.sftrace",
        status=RunStatus.COMPLETED,
    )
    recorded = bundle_digest(path)

    lines = (path / "events.jsonl").read_text().splitlines()
    event = json.loads(lines[1])
    event["input"]["query"] = "Pokhara flights"
    lines[1] = json.dumps(event)
    (path / "events.jsonl").write_text("\n".join(lines) + "\n")
    rewrite_integrity_record(path)

    # Rewriting the record with the payload leaves the record self-consistent.
    assert verify_bundle_integrity(path).status is IntegrityStatus.VERIFIED

    result = runner.invoke(app, ["validate", str(path), "--expect-digest", recorded])

    assert result.exit_code == 1
    assert "NO MATCH" in result.stdout


def test_validate_expect_digest_rejects_a_malformed_digest(tmp_path: Path) -> None:
    path = complete_trace().save(
        tmp_path / "valid.sftrace",
        status=RunStatus.COMPLETED,
    )

    result = runner.invoke(
        app, ["validate", str(path), "--expect-digest", "not-a-digest"]
    )

    assert result.exit_code == 2
    assert "not a SHA-256 hex digest" in result.stdout
