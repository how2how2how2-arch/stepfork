from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from stepfork import ToolCall, Trace
from stepfork.trace import RunEnd, RunStart, RunStatus, ToolResult, validate_bundle
from stepfork.trace.integrity import IntegrityStatus, verify_bundle_integrity


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
        ToolResult(name="search", output={"ok": True}, parent_id=call.id)
    )
    trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=result.id))
    return trace


def save_complete(tmp_path: Path) -> Path:
    return complete_trace().save(
        tmp_path / "complete.sftrace",
        status=RunStatus.COMPLETED,
    )


def test_new_bundle_verifies_successfully(tmp_path: Path) -> None:
    path = save_complete(tmp_path)

    result = verify_bundle_integrity(path)

    assert result.status is IntegrityStatus.VERIFIED
    assert result.verified_files == ["manifest.json", "events.jsonl", "redactions.json"]
    assert not result.issues


def test_editing_events_jsonl_causes_mismatch(tmp_path: Path) -> None:
    path = save_complete(tmp_path)
    lines = (path / "events.jsonl").read_text().splitlines()
    event = json.loads(lines[1])
    event["input"]["query"] = "Pokhara flights"
    lines[1] = json.dumps(event)
    (path / "events.jsonl").write_text("\n".join(lines) + "\n")

    result = verify_bundle_integrity(path)

    assert result.status is IntegrityStatus.MISMATCH
    assert result.issues[0].file == "events.jsonl"


def test_editing_manifest_json_causes_mismatch(tmp_path: Path) -> None:
    path = save_complete(tmp_path)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["agent_name"] = "other-agent"
    (path / "manifest.json").write_text(json.dumps(manifest))

    assert verify_bundle_integrity(path).issues[0].file == "manifest.json"


def test_editing_redactions_json_causes_mismatch(tmp_path: Path) -> None:
    path = save_complete(tmp_path)
    redactions = json.loads((path / "redactions.json").read_text())
    redactions["entries"] = []
    redactions["extra_safe_field"] = True
    (path / "redactions.json").write_text(json.dumps(redactions))

    assert verify_bundle_integrity(path).issues[0].file == "redactions.json"


def test_missing_integrity_file_returns_legacy_status(tmp_path: Path) -> None:
    path = save_complete(tmp_path)
    (path / "integrity.json").unlink()

    result = verify_bundle_integrity(path)

    assert result.status is IntegrityStatus.UNVERIFIED_LEGACY
    assert result.issues[0].code == "integrity_unverified"
    assert Trace.load(path).agent_name == "demo-agent"


def test_malformed_integrity_file_is_rejected(tmp_path: Path) -> None:
    path = save_complete(tmp_path)
    (path / "integrity.json").write_text("{")

    result = verify_bundle_integrity(path)

    assert result.status is IntegrityStatus.MISMATCH
    assert result.issues[0].code == "integrity_metadata_invalid"


def test_missing_required_digest_is_rejected(tmp_path: Path) -> None:
    path = save_complete(tmp_path)
    record = json.loads((path / "integrity.json").read_text())
    del record["files"]["events.jsonl"]
    (path / "integrity.json").write_text(json.dumps(record))

    result = verify_bundle_integrity(path)

    assert result.issues[0].code == "integrity_metadata_invalid"
    assert result.issues[0].file == "events.jsonl"


def test_unsupported_algorithm_is_rejected(tmp_path: Path) -> None:
    path = save_complete(tmp_path)
    record = json.loads((path / "integrity.json").read_text())
    record["algorithm"] = "md5"
    (path / "integrity.json").write_text(json.dumps(record))

    result = verify_bundle_integrity(path)

    assert result.issues[0].code == "integrity_algorithm_unsupported"


def test_invalid_sha256_digest_is_rejected(tmp_path: Path) -> None:
    path = save_complete(tmp_path)
    record = json.loads((path / "integrity.json").read_text())
    record["files"]["events.jsonl"] = "not-a-digest"
    (path / "integrity.json").write_text(json.dumps(record))

    result = verify_bundle_integrity(path)

    assert result.issues[0].code == "integrity_metadata_invalid"


def test_verification_does_not_mutate_bundle(tmp_path: Path) -> None:
    path = save_complete(tmp_path)
    before = {item.name: item.read_bytes() for item in path.iterdir() if item.is_file()}

    verify_bundle_integrity(path)

    after = {item.name: item.read_bytes() for item in path.iterdir() if item.is_file()}
    assert after == before


def test_structural_validation_still_works_with_integrity(tmp_path: Path) -> None:
    path = save_complete(tmp_path)

    assert validate_bundle(path).valid
    assert validate_bundle(path, verify_integrity=True).valid


def test_legacy_bundle_is_structurally_valid_but_not_verified(tmp_path: Path) -> None:
    path = save_complete(tmp_path)
    legacy = tmp_path / "legacy.sftrace"
    shutil.copytree(path, legacy)
    (legacy / "integrity.json").unlink()

    assert validate_bundle(legacy).valid
    assert not validate_bundle(legacy, verify_integrity=True).valid


def test_record_naming_a_file_outside_the_bundle_is_rejected(tmp_path: Path) -> None:
    """An entry in the record cannot reach outside the bundle it describes."""
    path = save_complete(tmp_path)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("not part of any bundle\n", encoding="utf-8")
    record_path = path / "integrity.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["files"]["../outside-secret.txt"] = hashlib.sha256(
        outside.read_bytes()
    ).hexdigest()
    record_path.write_text(json.dumps(record), encoding="utf-8", newline="")

    result = verify_bundle_integrity(path)

    assert result.status is IntegrityStatus.MISMATCH
    assert "../outside-secret.txt" not in result.verified_files
    assert [issue.code for issue in result.issues] == ["integrity_metadata_invalid"]


def test_record_naming_a_file_the_bundle_does_not_cover_is_rejected(
    tmp_path: Path,
) -> None:
    """A name the bundle format does not define is not a name to verify."""
    path = save_complete(tmp_path)
    (path / "extra.json").write_text("{}\n", encoding="utf-8", newline="")
    record_path = path / "integrity.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["files"]["extra.json"] = hashlib.sha256(b"{}\n").hexdigest()
    record_path.write_text(json.dumps(record), encoding="utf-8", newline="")

    result = verify_bundle_integrity(path)

    assert result.status is IntegrityStatus.MISMATCH
    assert "extra.json" not in result.verified_files
    assert [issue.code for issue in result.issues] == ["integrity_metadata_invalid"]
