"""A forked bundle records what was replaced, and older bundles still verify."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stepfork import ToolCall, Trace
from stepfork.fork import (
    ForkPoint,
    OverrideSpec,
    fork_trace,
    read_fork_provenance,
)
from stepfork.trace import RunEnd, RunStart, RunStatus, ToolResult
from stepfork.trace.integrity import (
    INTEGRITY_FILE,
    IntegrityStatus,
    verify_bundle_integrity,
)
from stepfork.trace.storage import FORK_FILE, TraceStorageError


def build_trace() -> Trace:
    """Build a trace with one retrieval-style tool result that can be replaced."""
    trace = Trace(agent_name="test-agent")
    start = trace.add(RunStart(input={"query": "test"}))

    call = trace.add(
        ToolCall(name="search", input={"q": "flights"}, parent_id=start.id)
    )
    result = trace.add(
        ToolResult(name="search", output={"count": 5}, parent_id=call.id)
    )
    trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=result.id))
    return trace


def save_trace(tmp_path: Path) -> Path:
    """Save the trace as a bundle and return its path."""
    return build_trace().save(tmp_path / "run.sftrace", status=RunStatus.COMPLETED)


def fork_with_override(parent: Path, output: Path) -> str:
    """Fork ``parent`` past every event, replacing the tool result's output."""
    result = fork_trace(
        parent,
        ForkPoint(event_id=None, step=3),
        overrides=[
            OverrideSpec(step=2, output={"count": 99}),
        ],
        output_path=output,
    )
    assert result.success, result.error_message
    assert result.forked_trace_path is not None
    return result.forked_trace_path


def test_forked_bundle_records_provenance(tmp_path: Path) -> None:
    """The saved bundle carries the fork it came from and the events it replaced."""
    parent = save_trace(tmp_path)
    forked = Path(fork_with_override(parent, tmp_path / "forked.sftrace"))

    assert (forked / FORK_FILE).is_file()

    provenance = read_fork_provenance(forked)
    assert provenance is not None
    assert provenance.fork_info.parent_trace_path == str(parent)
    assert provenance.fork_info.parent_run_id == Trace.load(parent).run_id
    assert provenance.fork_info.fork_point_step == 3
    assert provenance.fork_info.overrides_applied == 1
    # The count alone cannot say which output changed; the id can.
    assert provenance.overridden_event_ids == [Trace.load(parent).events[2].id]


def test_bundle_written_without_a_fork_has_no_provenance(tmp_path: Path) -> None:
    """A recorded run carries no provenance file and verifies as before."""
    path = save_trace(tmp_path)

    assert not (path / FORK_FILE).exists()
    assert read_fork_provenance(path) is None

    result = verify_bundle_integrity(path)
    assert result.status is IntegrityStatus.VERIFIED
    assert result.verified_files == ["manifest.json", "events.jsonl", "redactions.json"]


def test_integrity_covers_recorded_provenance(tmp_path: Path) -> None:
    """Provenance is named in the integrity record, so changing it is detected."""
    forked = Path(fork_with_override(save_trace(tmp_path), tmp_path / "forked.sftrace"))

    verified = verify_bundle_integrity(forked)
    assert verified.status is IntegrityStatus.VERIFIED
    assert FORK_FILE in verified.verified_files

    provenance = read_fork_provenance(forked)
    assert provenance is not None
    tampered = provenance.model_dump(mode="json")
    tampered["fork_info"]["overrides_applied"] = 0
    (forked / FORK_FILE).write_text(json.dumps(tampered), encoding="utf-8", newline="")

    result = verify_bundle_integrity(forked)
    assert result.status is IntegrityStatus.MISMATCH
    assert [issue.file for issue in result.issues] == [FORK_FILE]


def test_provenance_present_but_unrecorded_is_reported(tmp_path: Path) -> None:
    """Dropping the digest while keeping the file does not pass verification."""
    forked = Path(fork_with_override(save_trace(tmp_path), tmp_path / "forked.sftrace"))

    integrity_path = forked / INTEGRITY_FILE
    record = json.loads(integrity_path.read_text(encoding="utf-8"))
    del record["files"][FORK_FILE]
    integrity_path.write_text(json.dumps(record), encoding="utf-8", newline="")

    result = verify_bundle_integrity(forked)
    assert result.status is IntegrityStatus.MISMATCH
    assert [issue.code for issue in result.issues] == ["integrity_unlisted_file"]


def test_recorded_provenance_removed_is_reported(tmp_path: Path) -> None:
    """Deleting the provenance file the record names is a missing file."""
    forked = Path(fork_with_override(save_trace(tmp_path), tmp_path / "forked.sftrace"))
    (forked / FORK_FILE).unlink()

    result = verify_bundle_integrity(forked)
    assert result.status is IntegrityStatus.MISMATCH
    assert [issue.code for issue in result.issues] == ["missing_file"]
    assert [issue.file for issue in result.issues] == [FORK_FILE]


def test_invalid_provenance_is_rejected(tmp_path: Path) -> None:
    """A provenance file that cannot be read raises rather than reading as absent."""
    forked = Path(fork_with_override(save_trace(tmp_path), tmp_path / "forked.sftrace"))
    (forked / FORK_FILE).write_text("{not json", encoding="utf-8", newline="")

    with pytest.raises(TraceStorageError):
        read_fork_provenance(forked)
