"""Filesystem persistence for `.sftrace` directory bundles."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from stepfork.trace.hashing import hash_event_payloads
from stepfork.trace.manifest import (
    EnvironmentInfo,
    FailureInfo,
    RunStatus,
    TraceManifest,
    TraceTotals,
)
from stepfork.trace.models import Event, EventType, Trace
from stepfork.trace.redaction import RedactionManifest, prepare_trace_for_persistence

SCHEMA_VERSION = "0.1"
MANIFEST_FILE = "manifest.json"
EVENTS_FILE = "events.jsonl"
REDACTIONS_FILE = "redactions.json"
REQUIRED_FILES = (MANIFEST_FILE, EVENTS_FILE, REDACTIONS_FILE)

_event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


class TraceStorageError(ValueError):
    """Raised when a `.sftrace` bundle cannot be saved or loaded."""


def build_manifest(
    trace: Trace,
    *,
    status: RunStatus | str | None = None,
    failure: FailureInfo | None = None,
    environment: EnvironmentInfo | None = None,
) -> TraceManifest:
    """Construct a manifest from an in-memory trace."""
    manifest_status = RunStatus(status) if status is not None else trace.status
    manifest_failure = trace.failure if failure is None else failure
    manifest_environment = trace.environment if environment is None else environment
    totals = _computed_totals(trace)

    return TraceManifest(
        run_id=trace.run_id,
        agent_name=trace.agent_name,
        created_at=trace.created_at,
        status=manifest_status,
        failure=manifest_failure,
        environment=manifest_environment,
        totals=totals,
    )


def save_trace(
    trace: Trace,
    path: Path,
    *,
    status: RunStatus | str | None = None,
    failure: FailureInfo | None = None,
    environment: EnvironmentInfo | None = None,
    overwrite: bool = False,
) -> Path:
    """Persist a trace as a `.sftrace` directory and return its path."""
    path = path.expanduser()
    _require_sftrace_suffix(path)
    manifest = build_manifest(
        trace,
        status=status,
        failure=failure,
        environment=environment,
    )
    prepared = prepare_trace_for_persistence(trace, manifest)
    persisted_events = [hash_event_payloads(event) for event in prepared.trace.events]

    if path.exists() and not path.is_dir():
        raise TraceStorageError(f"{path} exists and is not a directory")
    if path.exists() and any(path.iterdir()):
        if not overwrite:
            raise TraceStorageError(f"{path} already exists and is not empty")
        if not _has_required_bundle_files(path):
            raise TraceStorageError(f"{path} is not a valid .sftrace bundle")

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{path.name}.tmp-{uuid4().hex}"
    temporary.mkdir()

    try:
        _write_manifest(temporary / MANIFEST_FILE, prepared.manifest)
        _write_events(temporary / EVENTS_FILE, persisted_events)
        _write_redactions(temporary / REDACTIONS_FILE, prepared.redactions)
        from stepfork.trace.integrity import write_bundle_integrity

        write_bundle_integrity(temporary)

        if path.exists():
            if any(path.iterdir()):
                backup = parent / f".{path.name}.bak-{uuid4().hex}"
                path.rename(backup)
                try:
                    temporary.rename(path)
                except Exception:
                    backup.rename(path)
                    raise
                shutil.rmtree(backup)
            else:
                path.rmdir()
                temporary.rename(path)
        else:
            temporary.rename(path)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise

    return path


def load_trace(path: Path) -> Trace:
    """Load a `.sftrace` directory without repairing invalid data."""
    path = path.expanduser()
    _require_sftrace_suffix(path)
    if not path.exists():
        raise TraceStorageError(f"{path} does not exist")
    if not path.is_dir():
        raise TraceStorageError(f"{path} is not a directory")

    for filename in REQUIRED_FILES:
        file_path = path / filename
        if not file_path.exists():
            raise TraceStorageError(f"missing required file: {file_path}")
        if not file_path.is_file():
            raise TraceStorageError(f"required path is not a file: {file_path}")

    manifest = _read_manifest(path / MANIFEST_FILE)
    events = _read_events(path / EVENTS_FILE)
    _read_redactions(path / REDACTIONS_FILE)

    return Trace(
        run_id=manifest.run_id,
        agent_name=manifest.agent_name,
        created_at=manifest.created_at,
        status=manifest.status,
        failure=manifest.failure,
        environment=manifest.environment,
        totals=manifest.totals,
        events=events,
    )


def _computed_totals(trace: Trace) -> TraceTotals:
    return TraceTotals(
        events=len(trace.events),
        llm_calls=sum(event.type is EventType.LLM_REQUEST for event in trace.events),
        tool_calls=sum(event.type is EventType.TOOL_CALL for event in trace.events),
        cost_usd=trace.totals.cost_usd,
        duration_ms=trace.totals.duration_ms,
    )


def _require_sftrace_suffix(path: Path) -> None:
    if path.suffix != ".sftrace":
        raise TraceStorageError("trace bundle path must end with .sftrace")


def _has_required_bundle_files(path: Path) -> bool:
    return all((path / filename).is_file() for filename in REQUIRED_FILES)


def _write_manifest(path: Path, manifest: TraceManifest) -> None:
    # newline="" keeps the bundle byte-identical on every platform. Bundle
    # digests are SHA-256 over file bytes (trace/integrity.py::_hash_file and
    # export/receipt.py::bundle_digest), so letting the platform translate
    # "\n" to "\r\n" would make a bundle written on Windows fail
    # verification everywhere else, and fail on Windows for a bundle written
    # elsewhere.
    path.write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )


def _write_events(path: Path, events: list[Event]) -> None:
    # newline="" for the same reason as _write_manifest: events.jsonl is
    # hashed as bytes.
    with path.open("w", encoding="utf-8", newline="") as file:
        for event in events:
            file.write(_event_adapter.dump_json(event).decode("utf-8"))
            file.write("\n")


def _write_redactions(path: Path, redactions: RedactionManifest) -> None:
    # newline="" for the same reason as _write_manifest: redactions.json is
    # hashed as bytes.
    path.write_text(
        redactions.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )


def _read_manifest(path: Path) -> TraceManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TraceStorageError(f"{path}: invalid JSON: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise TraceStorageError(f"{path}: invalid manifest: expected object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise TraceStorageError(
            f"{path}: unsupported schema version {payload.get('schema_version')!r}"
        )

    try:
        return TraceManifest.model_validate(payload)
    except ValidationError as exc:
        raise TraceStorageError(f"{path}: invalid manifest") from exc


def _read_events(path: Path) -> list[Event]:
    events: list[Event] = []
    with path.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if line.strip() == "":
                raise TraceStorageError(f"{path}:{line_number}: blank JSONL line")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TraceStorageError(
                    f"{path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            try:
                events.append(_event_adapter.validate_python(payload))
            except ValidationError as exc:
                raise TraceStorageError(f"{path}:{line_number}: invalid event") from exc
    return events


def _read_redactions(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TraceStorageError(f"{path}: invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise TraceStorageError(f"{path}: invalid redactions object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise TraceStorageError(
            f"{path}: unsupported redactions schema version "
            f"{payload.get('schema_version')!r}"
        )
    try:
        RedactionManifest.model_validate(payload)
    except ValidationError as exc:
        raise TraceStorageError(f"{path}: invalid redactions metadata") from exc
