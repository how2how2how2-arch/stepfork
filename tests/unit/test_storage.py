from __future__ import annotations

import json
from pathlib import Path

import pytest

from stepfork import FailureInfo, ToolCall, Trace
from stepfork.trace import (
    EnvironmentInfo,
    ErrorEvent,
    RunEnd,
    RunStart,
    RunStatus,
    ToolResult,
    TraceStorageError,
    TraceTotals,
)


def test_save_creates_required_files_and_json(tmp_path: Path) -> None:
    trace = Trace(agent_name="demo-agent")
    trace.add(ToolCall(name="search", input={"query": "Kathmandu flights"}))

    destination = trace.save(tmp_path / "demo.sftrace")

    assert destination == tmp_path / "demo.sftrace"
    assert (destination / "manifest.json").is_file()
    assert (destination / "events.jsonl").is_file()
    assert (destination / "redactions.json").is_file()
    assert (destination / "integrity.json").is_file()
    assert json.loads((destination / "manifest.json").read_text())
    assert json.loads((destination / "redactions.json").read_text()) == {
        "schema_version": "0.1",
        "entries": [],
    }


def test_events_file_contains_one_event_per_line(tmp_path: Path) -> None:
    trace = Trace(agent_name="demo-agent")
    trace.add(ToolCall(name="search", input={"query": "Kathmandu flights"}))
    trace.add(ToolCall(name="fetch", input={"url": "https://example.com"}))

    destination = trace.save(tmp_path / "demo.sftrace")

    lines = (destination / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["type"] for line in lines] == ["tool_call", "tool_call"]


def test_unicode_and_nested_json_survive_round_trip(tmp_path: Path) -> None:
    trace = Trace(agent_name="demo-agent")
    payload = {
        "query": "काठमाडौँ flights",
        "filters": {"stops": [0, 1], "refundable": True},
    }
    trace.add(ToolCall(name="search", input=payload))

    loaded = Trace.load(trace.save(str(tmp_path / "unicode.sftrace")))

    event = loaded.events[0]
    assert isinstance(event, ToolCall)
    assert event.input == payload


def test_save_redacts_persisted_secret_without_mutating_trace(tmp_path: Path) -> None:
    secret = "sk-syntheticstorage123456"
    trace = Trace(agent_name="demo-agent")
    event = trace.add(ToolCall(name="search", input={"api_key": secret}))

    destination = trace.save(tmp_path / "secret.sftrace")

    assert event.input == {"api_key": secret}
    persisted = "\n".join(item.read_text() for item in destination.iterdir())
    assert secret not in persisted
    loaded = Trace.load(destination)
    assert loaded.events[0].model_dump(mode="json")["input"]["api_key"] == (
        "[REDACTED]"
    )


def test_path_accepts_str_and_path(tmp_path: Path) -> None:
    first = Trace(agent_name="demo-agent")
    first.save(str(tmp_path / "first.sftrace"))

    second = Trace(agent_name="demo-agent")
    second.save(tmp_path / "second.sftrace")

    assert Trace.load(str(tmp_path / "first.sftrace")).agent_name == "demo-agent"
    assert Trace.load(tmp_path / "second.sftrace").agent_name == "demo-agent"


def test_incorrect_extension_rejected(tmp_path: Path) -> None:
    with pytest.raises(TraceStorageError, match=r"\.sftrace"):
        Trace(agent_name="demo-agent").save(tmp_path / "demo")

    with pytest.raises(TraceStorageError, match=r"\.sftrace"):
        Trace.load(tmp_path / "demo")


def test_existing_destination_requires_overwrite(tmp_path: Path) -> None:
    trace = Trace(agent_name="demo-agent")
    destination = trace.save(tmp_path / "demo.sftrace")

    with pytest.raises(TraceStorageError, match="already exists"):
        trace.save(destination)

    trace.save(destination, overwrite=True)
    assert (destination / "manifest.json").is_file()


def test_overwrite_rejects_unrelated_directory(tmp_path: Path) -> None:
    destination = tmp_path / "notes.sftrace"
    destination.mkdir()
    (destination / "notes.txt").write_text("not a trace")

    with pytest.raises(TraceStorageError, match="not a valid"):
        Trace(agent_name="demo-agent").save(destination, overwrite=True)

    assert (destination / "notes.txt").read_text() == "not a trace"


def test_missing_files_produce_useful_errors(tmp_path: Path) -> None:
    destination = Trace(agent_name="demo-agent").save(tmp_path / "demo.sftrace")
    (destination / "redactions.json").unlink()

    with pytest.raises(TraceStorageError, match=r"redactions\.json"):
        Trace.load(destination)


def test_malformed_jsonl_reports_file_and_line(tmp_path: Path) -> None:
    destination = Trace(agent_name="demo-agent").save(tmp_path / "demo.sftrace")
    (destination / "events.jsonl").write_text('{"type": "tool_call"\n')

    with pytest.raises(TraceStorageError, match=r"events\.jsonl:1"):
        Trace.load(destination)


def test_unknown_event_type_is_rejected(tmp_path: Path) -> None:
    trace = Trace(agent_name="demo-agent")
    trace.add(ToolCall(name="search", input={"query": "Kathmandu flights"}))
    destination = trace.save(tmp_path / "demo.sftrace")
    event = json.loads((destination / "events.jsonl").read_text())
    event["type"] = "unknown"
    (destination / "events.jsonl").write_text(json.dumps(event) + "\n")

    with pytest.raises(TraceStorageError, match="invalid event"):
        Trace.load(destination)


def test_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    destination = Trace(agent_name="demo-agent").save(tmp_path / "demo.sftrace")
    manifest = json.loads((destination / "manifest.json").read_text())
    manifest["schema_version"] = "9.9"
    (destination / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(TraceStorageError, match="unsupported schema version"):
        Trace.load(destination)


def test_manifest_metadata_survives_round_trip(tmp_path: Path) -> None:
    trace = Trace(
        agent_name="demo-agent",
        environment=EnvironmentInfo(python="3.12"),
        totals=TraceTotals(cost_usd=0.25, duration_ms=123),
    )
    trace.add(ErrorEvent(error_type="ToolError", message="boom"))
    failure = FailureInfo(type="ToolError", message="boom", step=0)

    loaded = Trace.load(
        trace.save(
            tmp_path / "failure.sftrace",
            status=RunStatus.FAILED,
            failure=failure,
        )
    )

    assert loaded.status is RunStatus.FAILED
    assert loaded.failure == failure
    assert loaded.environment.python == "3.12"
    assert loaded.totals.cost_usd == 0.25
    assert loaded.totals.duration_ms == 123


def test_event_order_and_identity_survive_round_trip(tmp_path: Path) -> None:
    trace = Trace(agent_name="demo-agent")
    start = trace.add(RunStart(input={"prompt": "book"}))
    call = trace.add(
        ToolCall(
            name="search",
            input={"query": "Kathmandu flights"},
            parent_id=start.id,
            input_hash="input123",
        )
    )
    result = trace.add(
        ToolResult(
            name="search",
            output={"ok": True},
            parent_id=call.id,
            output_hash="output123",
        )
    )
    trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=result.id))

    loaded = Trace.load(
        trace.save(tmp_path / "complete.sftrace", status=RunStatus.COMPLETED)
    )

    assert [event.id for event in loaded.events] == [event.id for event in trace.events]
    assert [event.step for event in loaded.events] == [0, 1, 2, 3]
    assert loaded.events[1].parent_id == start.id
    assert loaded.events[1].input_hash != "input123"
    assert loaded.events[2].output_hash != "output123"
    assert loaded.events[1].input_hash is not None
    assert loaded.events[2].output_hash is not None


def test_save_rejects_non_directory_destination(tmp_path: Path) -> None:
    destination = tmp_path / "file.sftrace"
    destination.write_text("occupied", encoding="utf-8")

    with pytest.raises(TraceStorageError, match="not a directory"):
        Trace(agent_name="demo-agent").save(destination)


def test_load_rejects_file_instead_of_directory(tmp_path: Path) -> None:
    destination = tmp_path / "file.sftrace"
    destination.write_text("nope", encoding="utf-8")

    with pytest.raises(TraceStorageError, match="not a directory"):
        Trace.load(destination)


def test_load_rejects_required_file_as_directory(tmp_path: Path) -> None:
    destination = tmp_path / "demo.sftrace"
    destination.mkdir()
    (destination / "manifest.json").mkdir()
    (destination / "events.jsonl").write_text(
        '{"type": "run_start", "status": "ok", "run_id": "r", "step": 0}\n',
        encoding="utf-8",
    )
    (destination / "redactions.json").write_text(
        '{"schema_version": "0.1", "entries": []}',
        encoding="utf-8",
    )

    with pytest.raises(TraceStorageError, match="not a file"):
        Trace.load(destination)


def test_load_rejects_blank_jsonl_line(tmp_path: Path) -> None:
    destination = tmp_path / "demo.sftrace"
    Trace(agent_name="demo-agent").save(destination)
    (destination / "events.jsonl").write_text("\n", encoding="utf-8")

    with pytest.raises(TraceStorageError, match="blank JSONL"):
        Trace.load(destination)


def test_load_rejects_invalid_manifest_json(tmp_path: Path) -> None:
    destination = tmp_path / "demo.sftrace"
    Trace(agent_name="demo-agent").save(destination)
    (destination / "manifest.json").write_text("{nope", encoding="utf-8")

    with pytest.raises(TraceStorageError, match="invalid JSON"):
        Trace.load(destination)


def test_load_rejects_non_object_manifest(tmp_path: Path) -> None:
    destination = tmp_path / "demo.sftrace"
    Trace(agent_name="demo-agent").save(destination)
    (destination / "manifest.json").write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(TraceStorageError, match="expected object"):
        Trace.load(destination)


def test_load_rejects_invalid_manifest_payload(tmp_path: Path) -> None:
    destination = tmp_path / "demo.sftrace"
    Trace(agent_name="demo-agent").save(destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    manifest["agent_name"] = 5
    (destination / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(TraceStorageError, match="invalid manifest"):
        Trace.load(destination)


def test_load_rejects_invalid_redactions_json(tmp_path: Path) -> None:
    destination = tmp_path / "demo.sftrace"
    Trace(agent_name="demo-agent").save(destination)
    (destination / "redactions.json").write_text("{nope", encoding="utf-8")

    with pytest.raises(TraceStorageError, match="invalid JSON"):
        Trace.load(destination)


def test_load_rejects_non_object_redactions(tmp_path: Path) -> None:
    destination = tmp_path / "demo.sftrace"
    Trace(agent_name="demo-agent").save(destination)
    (destination / "redactions.json").write_text("42", encoding="utf-8")

    with pytest.raises(TraceStorageError, match="invalid redactions object"):
        Trace.load(destination)


def test_load_rejects_unsupported_redactions_schema(tmp_path: Path) -> None:
    destination = tmp_path / "demo.sftrace"
    Trace(agent_name="demo-agent").save(destination)
    (destination / "redactions.json").write_text(
        '{"schema_version": "9.9", "entries": []}',
        encoding="utf-8",
    )

    with pytest.raises(TraceStorageError, match="unsupported redactions"):
        Trace.load(destination)


def test_overwrite_roundtrip_through_backup(tmp_path: Path) -> None:
    destination = tmp_path / "demo.sftrace"
    trace = Trace(agent_name="demo-agent")
    trace.save(destination)

    updated = Trace(agent_name="overwritten")
    updated.add(ToolCall(name="search", input={"query": "Kathmandu flights"}))
    updated.save(destination, overwrite=True)

    loaded = Trace.load(destination)
    assert loaded.agent_name == "overwritten"
    assert loaded.totals.tool_calls == 1
    assert not list(tmp_path.glob("*.bak-*"))
    assert not list(tmp_path.glob(".demo.sftrace.bak-*"))


# --- Bundle files must be byte-identical across platforms -------------------
#
# A bundle's files are hashed as bytes (trace/integrity.py::_hash_file and
# export/receipt.py::bundle_digest), so a platform that rewrites "\n" to "\r\n"
# while writing changes every recorded digest. The writers in trace/storage.py
# therefore pass newline="".


def test_saved_bundle_files_contain_no_crlf(tmp_path: Path) -> None:
    """Saved bundle files use LF only, never CRLF.

    Note on what this test can and cannot show: on POSIX the platform's
    newline translation is a no-op, so this passes with or without the
    newline="" argument. It fails on Windows if the argument is removed, which
    is the mutation it guards. The companion test below shows why the guard is
    worth having at all, on any platform.

    The name list is asserted to equal the files the bundle actually writes,
    rather than being trusted. A hand-typed subset is how integrity.json came
    to be missing here: every writer looked guarded while one was not, and the
    omission was invisible because the list and the bundle grew apart. Adding
    a bundle file now fails this test until it is covered.
    """
    trace = Trace(agent_name="line-endings")
    trace.add(RunStart(input={"prompt": "two\nlines"}))
    trace.add(RunEnd(run_status=RunStatus.COMPLETED))

    destination = trace.save(tmp_path / "endings.sftrace")

    names = ("manifest.json", "events.jsonl", "redactions.json", "integrity.json")
    written = {path.name for path in destination.iterdir() if path.is_file()}
    assert written == set(names), (
        f"bundle writes files this test does not check: {sorted(written - set(names))}"
    )

    for name in names:
        payload = (destination / name).read_bytes()
        assert b"\r\n" not in payload, f"{name} contains CRLF"
        assert b"\n" in payload, f"{name} contains no line separator at all"


def test_bundle_digest_changes_when_a_file_gains_crlf(tmp_path: Path) -> None:
    """Rewriting newlines changes the digest, which is why the writer pins them.

    This is the stake behind the previous test: it demonstrates that the
    digest covers line endings rather than abstracting over them. It passes
    before and after the newline fix -- it describes the mechanism, not the
    fix -- and exists so the requirement is visible to anyone editing the
    writers.
    """
    from stepfork.export.receipt import bundle_digest

    trace = Trace(agent_name="digest-lines")
    trace.add(RunStart(input={"prompt": "hello"}))
    destination = trace.save(tmp_path / "digest.sftrace")

    before = bundle_digest(destination)

    manifest = destination / "manifest.json"
    manifest.write_bytes(manifest.read_bytes().replace(b"\n", b"\r\n"))

    assert bundle_digest(destination) != before


def test_gitattributes_pins_bundle_files_to_lf() -> None:
    """A Windows checkout must not rewrite bundle files to CRLF.

    git's default core.autocrlf on Windows converts text files at checkout,
    which corrupts the recorded digests before any test runs. The rule in
    .gitattributes is what prevents that, so this asserts the rule resolves
    for a committed bundle file. Unlike the writer assertion above, this one
    fails on every platform if the rule is missing or misspelled: `*.sftrace/*`
    and `*.sftrace/**/*` do not match nested files, `**/*.sftrace/**` does.
    """
    import shutil
    import subprocess

    repo = Path(__file__).resolve().parents[2]
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    if not (repo / ".git").exists():
        pytest.skip("not running from a git checkout")

    relative = "tests/golden/successful_run.sftrace/manifest.json"
    if not (repo / relative).is_file():
        pytest.skip(f"{relative} is missing")

    completed = subprocess.run(
        [git, "check-attr", "eol", "--", relative],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip().endswith("eol: lf"), completed.stdout
