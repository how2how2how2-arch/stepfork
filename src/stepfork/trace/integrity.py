"""Tamper-evident integrity records for `.sftrace` bundles."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stepfork.trace.storage import EVENTS_FILE, MANIFEST_FILE, REDACTIONS_FILE

INTEGRITY_FILE = "integrity.json"
INTEGRITY_ALGORITHM = "sha256"
INTEGRITY_FILES = (MANIFEST_FILE, EVENTS_FILE, REDACTIONS_FILE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class IntegrityStatus(StrEnum):
    """Result status for bundle integrity verification."""

    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNVERIFIED_LEGACY = "unverified_legacy"


class IntegrityIssue(BaseModel):
    """Machine-readable integrity issue."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    file: str | None = None


class IntegrityRecord(BaseModel):
    """Persisted integrity metadata for a `.sftrace` bundle."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    algorithm: str = INTEGRITY_ALGORITHM
    files: dict[str, str] = Field(default_factory=dict)


class IntegrityResult(BaseModel):
    """Structured bundle integrity verification result."""

    model_config = ConfigDict(extra="forbid")

    status: IntegrityStatus
    issues: list[IntegrityIssue] = Field(default_factory=list)
    verified_files: list[str] = Field(default_factory=list)


def is_sha256_digest(value: str) -> bool:
    """Whether ``value`` is a lowercase SHA-256 hex digest."""
    return _SHA256_RE.fullmatch(value) is not None


def write_bundle_integrity(path: Path) -> IntegrityRecord:
    """Write integrity metadata for the finalized bundle payload files."""
    record = IntegrityRecord(
        files={filename: _hash_file(path / filename) for filename in INTEGRITY_FILES}
    )
    # newline="" for the same reason as the other bundle writers in
    # trace/storage.py: this file is hashed as bytes by
    # export/receipt.py::bundle_digest, which walks every file in the bundle.
    # integrity.json does not hash itself (INTEGRITY_FILES covers the three
    # payload files), so on Windows this file would be written with CRLF and
    # change the bundle digest while verify_bundle_integrity still reported
    # "verified".
    (path / INTEGRITY_FILE).write_text(
        record.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    return record


def verify_bundle_integrity(path: str | Path) -> IntegrityResult:
    """Verify a bundle's integrity record."""
    bundle_path = Path(path)
    integrity_path = bundle_path / INTEGRITY_FILE
    if not integrity_path.exists():
        return IntegrityResult(
            status=IntegrityStatus.UNVERIFIED_LEGACY,
            issues=[
                IntegrityIssue(
                    code="integrity_unverified",
                    message="Bundle has no integrity.json metadata.",
                    file=INTEGRITY_FILE,
                )
            ],
        )
    if not integrity_path.is_file():
        return IntegrityResult(
            status=IntegrityStatus.MISMATCH,
            issues=[
                IntegrityIssue(
                    code="integrity_metadata_invalid",
                    message="integrity.json is not a file.",
                    file=INTEGRITY_FILE,
                )
            ],
        )

    try:
        payload = json.loads(integrity_path.read_text(encoding="utf-8"))
        record = IntegrityRecord.model_validate(payload)
    except json.JSONDecodeError:
        return _metadata_invalid("integrity.json is not valid JSON.")
    except ValidationError:
        return _metadata_invalid("integrity.json metadata is invalid.")

    issues = _validate_record(record)
    verified_files: list[str] = []
    if issues:
        return IntegrityResult(status=IntegrityStatus.MISMATCH, issues=issues)

    for filename in INTEGRITY_FILES:
        file_path = bundle_path / filename
        if not file_path.is_file():
            issues.append(
                IntegrityIssue(
                    code="missing_file",
                    message=f"{filename} is missing.",
                    file=filename,
                )
            )
            continue

        actual = _hash_file(file_path)
        expected = record.files[filename]
        if hmac.compare_digest(actual, expected):
            verified_files.append(filename)
        else:
            issues.append(
                IntegrityIssue(
                    code="integrity_mismatch",
                    message="SHA-256 mismatch.",
                    file=filename,
                )
            )

    return IntegrityResult(
        status=IntegrityStatus.VERIFIED if not issues else IntegrityStatus.MISMATCH,
        issues=issues,
        verified_files=verified_files,
    )


def _validate_record(record: IntegrityRecord) -> list[IntegrityIssue]:
    issues: list[IntegrityIssue] = []
    if record.algorithm != INTEGRITY_ALGORITHM:
        issues.append(
            IntegrityIssue(
                code="integrity_algorithm_unsupported",
                message=f"Unsupported integrity algorithm {record.algorithm!r}.",
                file=INTEGRITY_FILE,
            )
        )
        return issues

    for filename in INTEGRITY_FILES:
        digest = record.files.get(filename)
        if digest is None:
            issues.append(
                IntegrityIssue(
                    code="integrity_metadata_invalid",
                    message=f"Missing digest for {filename}.",
                    file=filename,
                )
            )
        elif not is_sha256_digest(digest):
            issues.append(
                IntegrityIssue(
                    code="integrity_metadata_invalid",
                    message=f"Invalid SHA-256 digest for {filename}.",
                    file=filename,
                )
            )
    return issues


def _metadata_invalid(message: str) -> IntegrityResult:
    return IntegrityResult(
        status=IntegrityStatus.MISMATCH,
        issues=[
            IntegrityIssue(
                code="integrity_metadata_invalid",
                message=message,
                file=INTEGRITY_FILE,
            )
        ],
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
