# Stepfork Trace Format

Stepfork stores traces as UTF-8 directory bundles with the `.sftrace` suffix.
The v0.1 layout is:

```text
example.sftrace/
├── manifest.json
├── events.jsonl
├── redactions.json
├── fork.json          (only for a bundle produced by `stepfork fork`)
└── integrity.json
```

`integrity.json` was added after the initial v0.1 storage work. Older bundles
without it remain loadable but are reported as `unverified_legacy`. `fork.json`
was added the same way: it is present only on a bundle written by a fork, and a
bundle without it loads and verifies exactly as before, so no schema version
change was needed.

## Manifest

`manifest.json` contains bundle-level metadata:

- `format`: always `stepfork`
- `schema_version`: currently `0.1`
- `run_id`
- `agent_name`
- `created_at`
- `status`: `running`, `completed`, or `failed`
- `failure`: required for failed runs
- `environment`
- `totals`

## Events

`events.jsonl` stores one complete JSON event per non-blank line. Blank lines
are invalid. Event order is significant and preserved by `Trace.load()`.

Supported event types:

- `run_start`
- `llm_request`
- `llm_response`
- `tool_call`
- `tool_result`
- `state_change`
- `error`
- `run_end`

Events share structural fields such as `id`, `run_id`, `parent_id`, `step`,
`timestamp`, `status`, and `replay_policy`. Payload hashes are stored in
`input_hash` and `output_hash` where applicable.

`input_hash` and `output_hash` are SHA-256 digests over Stepfork's canonical
JSON profile after redaction has been applied. During replay, dependency
inputs are re-normalized, redacted, canonicalized, and compared against the
recording. For LLM requests this means prompt payload changes, model changes,
and provider changes are treated as replay divergence.

## Redactions

`redactions.json` stores non-secret metadata describing redactions:

```json
{
  "schema_version": "0.1",
  "entries": [
    {
      "event_id": "evt_002",
      "path": "/input/api_key",
      "reason": "sensitive_key",
      "replacement": "[REDACTED]"
    }
  ]
}
```

Paths use JSON Pointer escaping for `~` and `/`. Redaction metadata must never
store original secrets, reversible encodings, or hashes of secret values.

## Fork provenance

`fork.json` records that a bundle was produced by `stepfork fork`, and what that
fork replaced:

```json
{
  "fork_info": {
    "parent_trace_path": "run.sftrace",
    "parent_run_id": "run_01J...",
    "fork_point_event_id": "evt_003",
    "fork_point_step": 3,
    "fork_timestamp": "2025-01-01T00:00:00Z",
    "overrides_applied": 1
  },
  "overridden_event_ids": ["evt_002"]
}
```

`overridden_event_ids` is what makes the record answerable per event: the count
in `fork_info` says how many outputs were replaced, and this list says which
ones. Without it a reader holding only the bundle cannot tell a recorded output
from one the fork replaced. `read_fork_provenance()` returns this record, or
`None` for a bundle that was not forked.

Bundles written before this file existed carry no provenance, and a bundle
without `fork.json` is indistinguishable from a recorded run. Provenance is
therefore evidence about a bundle, not a claim that this one was never forked.

## Integrity

`integrity.json` stores SHA-256 digests of the exact persisted bytes of:

- `manifest.json`
- `events.jsonl`
- `redactions.json`

`fork.json` is included when it is present, so recorded provenance is covered by
the same record and editing it is reported as a mismatch. A bundle whose
provenance file is present but absent from the record is reported as
`integrity_unlisted_file`, so the file cannot be kept while its digest is
dropped. Deleting both the file and its digest leaves a bundle that verifies as
an ordinary recorded run; that limit is the reason this is described as evidence
about the bundle rather than proof of its origin.

It does not include itself. This detects accidental modification and ordinary
tampering when the expected integrity record is trusted. It is not a digital
signature.

## Compatibility

The schema version is currently `0.1`, and the optional `fork.json` did not
change it. New readers should keep loading older valid v0.1 bundles where
possible. Missing integrity metadata means unverified
legacy, not verified. Older or hand-built events may also lack payload hashes;
they can still replay if their stored inputs exactly match the live calls, but
Stepfork reports computed fingerprints in diagnostics rather than silently
treating them as integrity-verified.
