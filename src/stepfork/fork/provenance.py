"""Persisted provenance for forked `.sftrace` bundles."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stepfork.fork.models import ForkInfo
from stepfork.trace.storage import FORK_FILE, TraceStorageError


class ForkProvenance(BaseModel):
    """What a forked bundle carries so a reader can tell what was replaced.

    ``ForkInfo`` describes the fork itself: which trace it came from, where the
    fork point was, and how many outputs were replaced. It does not say *which*
    outputs, and a count cannot answer that, so the overridden event ids are
    recorded next to it. A reader can then tell a recorded output from one this
    bundle replaced without loading the parent trace.
    """

    model_config = ConfigDict(extra="forbid")

    fork_info: ForkInfo
    overridden_event_ids: list[str] = Field(default_factory=list)


def write_fork_provenance(bundle_path: str | Path, provenance: ForkProvenance) -> Path:
    """Write a bundle's provenance file and return its path.

    Args:
        bundle_path: The `.sftrace` bundle to write into.
        provenance: The provenance to record.

    Returns:
        The path written.
    """
    path = Path(bundle_path) / FORK_FILE
    # newline="" for the same reason as the other bundle writers: these files
    # are hashed as bytes by export/receipt.py::bundle_digest, so a platform
    # newline translation would change the bundle digest.
    path.write_text(
        provenance.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    return path


def read_fork_provenance(bundle_path: str | Path) -> ForkProvenance | None:
    """Read a bundle's provenance, or ``None`` when the bundle was not forked.

    Args:
        bundle_path: The `.sftrace` bundle to read.

    Returns:
        The recorded provenance, or ``None`` for a bundle that carries none.

    Raises:
        TraceStorageError: If a provenance file exists but cannot be read.
    """
    path = Path(bundle_path) / FORK_FILE
    if not path.is_file():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TraceStorageError(f"{FORK_FILE} is not valid JSON") from exc

    try:
        return ForkProvenance.model_validate(payload)
    except ValidationError as exc:
        raise TraceStorageError(
            f"{FORK_FILE} is not valid provenance metadata"
        ) from exc
