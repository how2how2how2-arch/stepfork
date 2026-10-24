from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stepfork.trace import Event, JsonValue, Trace
from stepfork.trace.manifest import FailureInfo, RunStatus, TraceManifest
from stepfork.trace.models import RunStart
from stepfork.trace.redaction import (
    MANIFEST_REDACTION_ID,
    REDACTION_REPLACEMENT,
    RedactionError,
    prepare_trace_for_persistence,
    redact_json,
)

SYNTHETIC_BEARER = "Bearer synthetic_token_123456"
SYNTHETIC_OPENAI = "sk-synthetic1234567890"
SYNTHETIC_GHP = "ghp_synthetic1234567890"
SYNTHETIC_GHO = "gho_synthetic1234567890"
SYNTHETIC_PAT = "github_pat_synthetic1234567890"


def redacted(value: object) -> object:
    return redact_json(cast(JsonValue, value)).value


def test_sensitive_key_variants_are_redacted() -> None:
    payload = {
        "api_key": "synthetic-value",
        "API_KEY": "synthetic-value",
        "api-key": "synthetic-value",
        "accessToken": "synthetic-value",
        "clientSecret": "synthetic-value",
        "password": "synthetic-value",
        "authorization": SYNTHETIC_BEARER,
    }

    assert redacted(payload) == {key: REDACTION_REPLACEMENT for key in payload}


def test_non_sensitive_key_names_are_not_redacted() -> None:
    payload = {
        "keyboard": "mechanical",
        "monkey": "banana",
        "primary_key": "user_id",
    }

    assert redacted(payload) == payload


def test_nested_dictionaries_and_lists_are_redacted() -> None:
    payload = {
        "tool": "search",
        "args": [
            {"query": "Kathmandu flights"},
            {"authorization": SYNTHETIC_BEARER},
        ],
    }

    assert redacted(payload) == {
        "tool": "search",
        "args": [
            {"query": "Kathmandu flights"},
            {"authorization": REDACTION_REPLACEMENT},
        ],
    }


def test_string_credential_patterns_are_redacted_in_place() -> None:
    payload = {
        "message": (
            f"use {SYNTHETIC_OPENAI} and {SYNTHETIC_GHP} and "
            f"{SYNTHETIC_GHO} and {SYNTHETIC_PAT}"
        )
    }

    result = redact_json(payload)
    value = cast(dict[str, str], result.value)
    text = value["message"]

    assert SYNTHETIC_OPENAI not in text
    assert SYNTHETIC_GHP not in text
    assert SYNTHETIC_GHO not in text
    assert SYNTHETIC_PAT not in text
    assert text.count(REDACTION_REPLACEMENT) == 4


def test_json_pointer_paths_are_escaped() -> None:
    result = redact_json({"a/b": {"c~d": {"api_key": "synthetic"}}})

    assert result.findings[0].path == "/a~1b/c~0d/api_key"


def test_redaction_metadata_does_not_contain_raw_secret() -> None:
    result = redact_json({"api_key": "synthetic-secret-value"})
    metadata = [
        finding.model_dump() if hasattr(finding, "model_dump") else vars(finding)
        for finding in result.findings
    ]

    assert "synthetic-secret-value" not in repr(metadata)


def test_unicode_and_empty_values_are_preserved_when_not_sensitive() -> None:
    payload = {"city": "काठमाडौँ", "empty": ""}

    assert redacted(payload) == payload


def test_already_redacted_values_are_not_duplicated() -> None:
    result = redact_json({"api_key": REDACTION_REPLACEMENT})

    assert result.value == {"api_key": REDACTION_REPLACEMENT}
    assert result.findings == []


def test_original_input_object_is_not_mutated() -> None:
    payload = {"nested": {"api_key": "synthetic"}}
    original = deepcopy(payload)

    redact_json(payload)

    assert payload == original


@given(st.dictionaries(st.text(), st.text(), max_size=5))
def test_redaction_is_idempotent_for_string_maps(payload: dict[str, str]) -> None:
    once = redact_json(payload).value
    twice = redact_json(once).value

    assert once == twice


# --- Issue #29: exception messages, nested payload shapes, preserved values ---


def test_credential_in_exception_message_is_redacted() -> None:
    payload = {"error": "401 Unauthorized: Bearer synthetic_abcdefgh12345678"}

    result = redact_json(payload)

    text = cast(dict[str, str], result.value)["error"]
    assert "synthetic_abcdefgh12345678" not in text
    assert text == f"401 Unauthorized: Bearer {REDACTION_REPLACEMENT}"
    assert [(f.path, f.reason) for f in result.findings] == [
        ("/error", "credential_pattern")
    ]


def test_credential_inside_multiline_traceback_is_redacted() -> None:
    payload = {
        "error": (
            "Traceback (most recent call last):\n"
            '  File "agent.py", line 12, in call\n'
            f"    raise RuntimeError('{SYNTHETIC_GHP}')\n"
        )
    }

    result = redact_json(payload)
    text = cast(dict[str, str], result.value)["error"]
    assert SYNTHETIC_GHP not in text
    assert text.count(REDACTION_REPLACEMENT) == 1
    assert 'File "agent.py", line 12, in call' in text


def test_authorization_bearer_header_is_redacted_exactly_once() -> None:
    result = redact_json({"note": "Authorization: Bearer synthetic_abcdefgh12345678"})

    text = cast(dict[str, str], result.value)["note"]
    assert text == f"Authorization: Bearer {REDACTION_REPLACEMENT}"
    assert text.count(REDACTION_REPLACEMENT) == 1


def test_credentials_in_string_lists_are_redacted_per_item() -> None:
    result = redact_json({"log": ["ok", SYNTHETIC_OPENAI, "done"]})

    assert cast(dict[str, list[str]], result.value)["log"] == [
        "ok",
        REDACTION_REPLACEMENT,
        "done",
    ]
    assert [(f.path, f.reason) for f in result.findings] == [
        ("/log/1", "credential_pattern")
    ]


def test_secret_assigned_in_a_dotenv_style_line_is_redacted() -> None:
    result = redact_json({"note": f"OPENAI_API_KEY={SYNTHETIC_OPENAI}"})

    text = cast(dict[str, str], result.value)["note"]
    assert SYNTHETIC_OPENAI not in text
    assert text.startswith("OPENAI_API_KEY=")


def test_ordinary_values_are_preserved_exactly() -> None:
    payload: dict[str, object] = {
        "count": 3,
        "ratio": 0.5,
        "flag": True,
        "nil": None,
        "name": "alice",
        "empty": "",
        "city": "काठमाडौँ",
        "already": REDACTION_REPLACEMENT,
    }

    result = redact_json(cast(JsonValue, payload))

    assert result.value == payload
    assert result.findings == []


def test_empty_payload_produces_no_findings() -> None:
    result = redact_json({})

    assert result.value == {}
    assert result.findings == []


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("bare token key", {"token": "synthetic12345678"}),
        ("basic auth", {"note": "Authorization: Basic c3ludGhldGljOnBhc3N3b3Jk"}),
        ("aws access key", {"note": "AKIAIOSFODNN7EXAMPLE"}),
        ("slack token", {"note": "xoxb-123456789012-syntheticabcd"}),
        ("google api key", {"note": "AIzaSyA-synthetic1234567890abcdefg"}),
        ("gitlab pat", {"note": "glpat-synthetic12345678"}),
        (
            "private key block",
            {
                "note": (
                    "-----BEGIN RSA PRIVATE KEY-----MIIE-----END RSA PRIVATE KEY-----"
                )
            },
        ),
        ("word-adjacent sk-", {"note": f"prefix{SYNTHETIC_OPENAI}suffix"}),
        ("sk- below the 8-char floor", {"note": "sk-abcdefg"}),
    ],
)
def test_formats_outside_the_pattern_set_pass_through(
    label: str, payload: dict[str, str]
) -> None:
    """Pin today's known gap: README documents redaction as best-effort.

    These inputs are not redacted by the current pattern set. This is a
    regression pin, not an endorsement -- if a pattern is added for one of
    these formats, move that case to the redacted cases above.
    """
    result = redact_json(cast(JsonValue, payload))

    assert result.value == payload, label
    assert result.findings == [], label


# --- Issue #29: the persistence path, which had no direct coverage ---


def _manifest(
    status: RunStatus = RunStatus.COMPLETED,
    failure: FailureInfo | None = None,
) -> TraceManifest:
    return TraceManifest(
        run_id="run-1",
        agent_name="synthetic-agent",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        status=status,
        failure=failure,
    )


def _trace(events: list[Event]) -> Trace:
    return Trace(
        run_id="run-1",
        agent_name="synthetic-agent",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        status=RunStatus.COMPLETED,
        events=events,
    )


def test_prepare_trace_for_persistence_records_event_redactions() -> None:
    event = RunStart(
        id="evt-1",
        run_id="run-1",
        input={"api_key": "synthetic-secret", "city": "kathmandu"},
    )

    prepared = prepare_trace_for_persistence(_trace([event]), _manifest())

    persisted = prepared.trace.events[0].model_dump(mode="json")
    assert persisted["input"]["api_key"] == REDACTION_REPLACEMENT
    assert persisted["input"]["city"] == "kathmandu"
    assert persisted["redactions"] == ["/input/api_key"]
    assert [
        (entry.event_id, entry.path, entry.reason)
        for entry in prepared.redactions.entries
    ] == [("evt-1", "/input/api_key", "sensitive_key")]
    assert "synthetic-secret" not in json.dumps(persisted)


def test_prepare_trace_for_persistence_redacts_manifest_failure_message() -> None:
    manifest = _manifest(
        status=RunStatus.FAILED,
        failure=FailureInfo(
            type="AuthError",
            message="401 from provider: Bearer synthetic_abcdefgh12345678",
            step=0,
        ),
    )

    prepared = prepare_trace_for_persistence(_trace([]), manifest)

    assert prepared.manifest.failure is not None
    assert "synthetic_abcdefgh12345678" not in prepared.manifest.failure.message
    assert prepared.manifest.failure.message.endswith(REDACTION_REPLACEMENT)
    assert [(entry.event_id, entry.path) for entry in prepared.redactions.entries] == [
        (MANIFEST_REDACTION_ID, "/failure/message")
    ]


def test_prepare_trace_for_persistence_appends_to_existing_redactions() -> None:
    event = RunStart(
        id="evt-1",
        run_id="run-1",
        redactions=["/earlier/redaction"],
        input={"password": "synthetic-secret"},
    )

    prepared = prepare_trace_for_persistence(_trace([event]), _manifest())

    persisted = prepared.trace.events[0].model_dump(mode="json")
    assert persisted["redactions"] == ["/earlier/redaction", "/input/password"]


def test_prepare_trace_for_persistence_rejects_credential_in_protected_field() -> None:
    event = RunStart(id=SYNTHETIC_GHP, run_id="run-1", input={})

    with pytest.raises(RedactionError):
        prepare_trace_for_persistence(_trace([event]), _manifest())


def test_prepare_trace_for_persistence_does_not_mutate_the_input_trace() -> None:
    event = RunStart(id="evt-1", run_id="run-1", input={"api_key": "synthetic-secret"})
    trace = _trace([event])
    original = deepcopy(trace.model_dump(mode="json"))

    prepare_trace_for_persistence(trace, _manifest())

    assert trace.model_dump(mode="json") == original


def test_prepare_trace_for_persistence_is_idempotent_for_persisted_data() -> None:
    """Re-preparing an already prepared trace is stable, except for the manifest.

    The persisted trace and manifest are stable across a second pass, but the
    redaction manifest comes back empty: its entries are derived from this
    pass's findings, and an already-redacted payload produces none. The event's
    own ``redactions`` list still holds the path, so nothing is lost from the
    bundle, but a second pass over a loaded trace would report no redactions.
    Pinned here so that changing it is a deliberate decision.
    """
    event = RunStart(
        id="evt-1",
        run_id="run-1",
        input={"api_key": "synthetic-secret", "error": f"Bearer {SYNTHETIC_GHP}"},
    )

    once = prepare_trace_for_persistence(_trace([event]), _manifest())
    twice = prepare_trace_for_persistence(once.trace, once.manifest)

    assert once.trace.model_dump(mode="json") == twice.trace.model_dump(mode="json")
    assert once.manifest.model_dump(mode="json") == twice.manifest.model_dump(
        mode="json"
    )
    assert [entry.path for entry in once.redactions.entries] == [
        "/input/api_key",
        "/input/error",
    ]
    assert twice.redactions.entries == []
