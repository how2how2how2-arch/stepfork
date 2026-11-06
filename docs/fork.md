# Fork-at-Step Debugging

Fork-at-step debugging creates new traces at specific execution points, preserving
all valid history before the fork while allowing you to modify recorded outputs.
This enables "what-if" debugging scenarios where you can test how your agent would
behave with different tool results or LLM responses.

## Overview

When debugging a failed agent run, you often want to answer questions like:

- "What if this API call returned different data?"
- "What if the LLM gave a different response at this step?"
- "Can I isolate this failure to events after step 5?"

Fork-at-step allows you to:

1. **Select a fork point** by step number or event ID
2. **Preserve all history** up to that point with complete provenance
3. **Apply overrides** to modify tool results or LLM responses (Python API only)
4. **Create valid traces** that maintain structural integrity

## Quick Start

### CLI: Basic Fork

Fork a trace at a specific step:

```bash
# Fork at step 5
stepfork fork failure.sftrace --at-step 5 --output forked.sftrace

# Fork at a specific event ID
stepfork fork trace.sftrace --at-event evt_abc123 -o forked.sftrace
```

The forked trace contains all events from step 0 through the specified step
(inclusive), with a new run ID but preserved event IDs.

### Python API: Fork with Overrides

```python
from pathlib import Path
from stepfork.fork import fork_trace, ForkPoint, OverrideSpec

# Fork at step 5 and change a tool result
result = fork_trace(
    Path("failure.sftrace"),
    ForkPoint(event_id=None, step=5),
    overrides=[OverrideSpec(step=5, output={"status": "success", "value": 100})],
    output_path=Path("forked.sftrace"),
)

print(f"Fork succeeded: {result.success}")
print(f"Overrides applied: {result.overrides_applied}")
```

## Fork Points

A fork point identifies where to split the trace. You can specify either:

- **By step number**: `--at-step N` or `ForkPoint(event_id=None, step=N)`
- **By event ID**: `--at-event ID` or `ForkPoint(event_id="evt_...", step=None)`

The forked trace includes all events **up to and including** the fork point.

### Example

Original trace with 10 events (steps 0-9):
- Fork at step 5 → forked trace has 6 events (steps 0-5)
- Fork at step 0 → forked trace has 1 event (step 0, usually RunStart)
- Fork at step 9 → forked trace has all 10 events

## Override System

Overrides allow you to modify recorded outputs before continuing execution. This is
only available through the Python API.

### Supported Event Types

You can override:
- ✅ **tool_result** - Modify what a tool returned
- ✅ **llm_response** - Modify what an LLM generated

You **cannot** override:
- ❌ tool_call - Input arguments (not outputs)
- ❌ llm_request - Prompts (not responses)
- ❌ run_start, run_end, state_change, error events

### Override Targeting

Target overrides by step number or event ID:

```python
from stepfork.fork import OverrideSpec

# Override by step number
override1 = OverrideSpec(step=5, output={"new": "value"})

# Override by event ID (more specific)
override2 = OverrideSpec(
    event_id="evt_abc123", output={"count": 100, "items": ["a", "b"]}
)

# Event ID takes priority if both target the same event
overrides = [override1, override2]
```

### Override Priority

When multiple overrides could apply to the same event:
1. **Event ID override** takes priority
2. **Step override** used as fallback
3. Only one override actually applied per event

### Validation

Overrides are validated before the fork operation:

```python
from stepfork.fork import ForkError

try:
    result = fork_trace(
        trace_path,
        fork_point,
        overrides=[
            OverrideSpec(step=999, output={})  # Invalid step
        ],
    )
except ForkError as e:
    print(f"Override validation failed: {e}")
    # "invalid overrides: override targets step 999 but trace only has 10 steps"
```

Validation checks:
- ✅ Step numbers exist in the trace
- ✅ Event IDs exist in the trace
- ✅ Target events are overridable types (tool_result or llm_response)

## Complete Example

```python
from pathlib import Path
from stepfork import record, trace_tool
from stepfork.fork import fork_trace, ForkPoint, OverrideSpec


# 1. Record original trace
@trace_tool(name="get_value")
def get_value(x: int) -> dict[str, int]:
    return {"value": x}


with record("test-agent", output="original.sftrace"):
    result = get_value(42)
    print(f"Original: {result}")  # {"value": 42}

# 2. Fork and override the tool result
fork_result = fork_trace(
    Path("original.sftrace"),
    ForkPoint(event_id=None, step=2),  # step 2 is the ToolResult
    overrides=[OverrideSpec(step=2, output={"value": 999})],
    output_path=Path("forked.sftrace"),
)

# 3. Verify the fork
from stepfork.trace import Trace

forked = Trace.load("forked.sftrace")
tool_result = forked.events[2]
print(f"Forked: {tool_result.output}")  # {"value": 999}
```

## Provenance Tracking

Every forked trace maintains complete provenance metadata:

```python
from stepfork.fork import ForkEngine, ForkPoint

engine = ForkEngine(
    original_trace,
    ForkPoint(event_id=None, step=5),
    parent_trace_path="original.sftrace",
)

forked_trace, fork_info = engine.fork()

# Fork info contains:
print(f"Parent run ID: {fork_info.parent_run_id}")
print(f"Parent trace: {fork_info.parent_trace_path}")
print(f"Fork point step: {fork_info.fork_point_step}")
print(f"Fork point event: {fork_info.fork_point_event_id}")
print(f"Fork timestamp: {fork_info.fork_timestamp}")
print(f"Overrides applied: {fork_info.overrides_applied}")
```

Provenance ensures you can always trace a forked trace back to its origin.

## Trace Integrity

Forked traces maintain structural integrity:

### Run IDs

- **New run_id** generated for the forked trace
- **All events** in forked trace have the new run_id
- **Original event IDs** are preserved

```python
# Original trace
original = Trace.load("original.sftrace")
print(f"Original run_id: {original.run_id}")
# "run_abc123..."

# Forked trace
forked = Trace.load("forked.sftrace")
print(f"Forked run_id: {forked.run_id}")
# "run_xyz789..." (different!)

# Event IDs preserved
assert forked.events[0].id == original.events[0].id
assert forked.events[0].run_id == forked.run_id
```

### Parent Dependencies

The fork engine validates that no parent dependencies are broken:

```python
# Event structure:
# 0: RunStart
# 1: ToolCall (parent: 0)
# 2: ToolResult (parent: 1)
# 3: ToolCall (parent: 2)

# ✅ Fork at step 2 - includes events 0, 1, 2 (all dependencies satisfied)
# ❌ Cannot fork in a way that excludes a parent while keeping a child
```

If forking would break dependencies, `ForkError` is raised.

## CLI Reference

### `stepfork fork`

Create a forked trace at a specific execution point.

```bash
stepfork fork <trace-path> --at-step N [OPTIONS]
stepfork fork <trace-path> --at-event ID [OPTIONS]
```

**Arguments:**
- `<trace-path>` - Path to the `.sftrace` bundle to fork

**Options:**
- `--at-step N` - Fork at this step number (0-indexed)
- `--at-event ID` - Fork at this event ID
- `-o, --output PATH` - Output path (default: `{stem}-forked.sftrace`)
- `--overwrite` - Overwrite output if it exists

You must specify either `--at-step` or `--at-event`, but not both.

**Example:**

```bash
$ stepfork fork failure.sftrace --at-step 2

Stepfork Fork

Original trace:  failure.sftrace
Agent:           booking-agent
Events:          15
Fork at step:    2

Forking trace...

╭─────────────── Fork Report ────────────────╮
│   Original Events     15                   │
│   Forked Events       3                    │
│   Fork Point Step     2                    │
│   Fork Point Event    evt_abc123...        │
│   Events Excluded     12                   │
╰────────────────────────────────────────────╯

✓ Forked trace saved to: failure-forked.sftrace
```

## Python API Reference

### `fork_trace()`

High-level function to fork a trace.

```python
def fork_trace(
    trace: Trace | Path,
    fork_point: ForkPoint,
    *,
    overrides: list[OverrideSpec] | None = None,
    output_path: Path | None = None,
    overwrite: bool = False,
) -> ForkResult:
    """Fork a trace at a specific step.

    Args:
        trace: Trace object or path to trace file
        fork_point: Where to fork (step or event_id)
        overrides: Optional overrides to apply
        output_path: Where to save forked trace
        overwrite: Whether to overwrite existing file

    Returns:
        ForkResult with operation details

    Raises:
        ForkError: If fork operation fails
    """
```

### `ForkPoint`

Identifies where to fork.

```python
@dataclass
class ForkPoint:
    event_id: str | None
    step: int | None

    # Must specify exactly one of event_id or step
```

### `OverrideSpec`

Specifies an output override.

```python
class OverrideSpec(BaseModel):
    step: int | None = None
    event_id: str | None = None
    output: JsonValue

    # Must specify exactly one of step or event_id
    # Output can be any JSON-serializable value
```

### `ForkResult`

Result of a fork operation.

```python
@dataclass
class ForkResult:
    success: bool
    forked_trace_path: str | None
    original_event_count: int
    forked_event_count: int
    fork_point_step: int
    fork_point_event_id: str
    overrides_applied: int
    error_message: str | None = None
```

### `ForkEngine`

Low-level fork orchestrator.

```python
class ForkEngine:
    def __init__(
        self,
        trace: Trace,
        fork_point: ForkPoint,
        *,
        overrides: list[OverrideSpec] | None = None,
        parent_trace_path: str | None = None,
    ):
        """Initialize fork engine."""

    def fork(self) -> tuple[Trace, ForkInfo]:
        """Execute fork operation.

        Returns:
            (forked_trace, fork_info)

        Raises:
            ForkError: If fork fails
        """
```

## Use Cases

### 1. Isolate Failure Points

Fork at different steps to identify when a failure first appears:

```bash
# Test if failure happens before step 10
stepfork fork failure.sftrace --at-step 10 -o test-step10.sftrace
stepfork replay test-step10.sftrace --entrypoint agent:run

# Binary search to find exact failure point
```

### 2. Test Different Scenarios

Override tool results to test "what-if" scenarios:

```python
# What if the API returned more results?
fork_trace(
    "trace.sftrace",
    ForkPoint(step=5),
    overrides=[OverrideSpec(step=5, output={"results": [...100 items...]})]
)

# What if the database was empty?
fork_trace(
    "trace.sftrace",
    ForkPoint(step=3),
    overrides=[OverrideSpec(step=3, output={"rows": []})]
)
```

### 3. Simplify Test Cases

Fork to create minimal reproduction cases:

```bash
# Original trace has 50 events, but failure happens at step 8
stepfork fork failure.sftrace --at-step 8 -o minimal.sftrace

# Now minimal.sftrace is a 9-event reproducer
```

## Limitations

### What Fork-at-Step Does NOT Do

**❌ Does not resume execution**
- Fork creates a modified trace
- It does not automatically continue execution from the fork point
- Use the replay API separately if you want to continue execution

**❌ CLI does not support overrides**
- Overrides are only available via Python API
- CLI can only fork without modifications

**⚠️ Reports stale downstream outputs, but does not re-execute them**
- Fork names the retained recorded outputs that sit after an overridden step
- It does not work out which of them actually depend on the changed value,
  and it does not re-run anything: see
  [Downstream outputs after an override](#downstream-outputs-after-an-override)

**❌ Cannot override inputs**
- Can only override outputs (tool_result, llm_response)
- Cannot change tool call arguments or LLM prompts

### Downstream outputs after an override

An override replaces a recorded output at one step. Every recorded output
that was captured after that step but still before the fork point was
produced under the upstream state the override changed, so it is not
evidence for the modified execution path.

`fork_trace()` reports this on its result:

```python
result = fork_trace(
    trace,
    ForkPoint(step=6),
    overrides=[OverrideSpec(step=2, output={"count": 99})],
)
assert result.overridden_steps == (2,)
assert len(result.stale_event_ids) == 2  # the results at steps 4 and 6
```

The classification is an upper bound, not a dependency analysis. It marks
every recorded output after the earliest overridden step, whether or not
that output's inputs actually changed, and it does not mark the event the
override replaced, because that value is the override rather than a reused
recording.

What this does not give you: replay does not consult the classification
yet, so a call that appears as `"substituted"` in `executed_calls` is not
by itself proof that the output belongs to the modified path. Neither is
the classification persisted into a saved bundle, so it is only available
from the `fork_trace()` call that produced it.

### Replay Integration

Fork-at-step currently creates modified traces but does not integrate with replay.
To continue execution from a fork point:

```python
from stepfork.fork import fork_trace, ForkPoint, OverrideSpec
from stepfork.replay import ReplaySession

# 1. Fork with overrides
fork_result = fork_trace(
    "original.sftrace",
    ForkPoint(step=5),
    overrides=[OverrideSpec(step=5, output={"new": "value"})],
    output_path="forked.sftrace",
)

# 2. Replay from the forked trace
# Note: This replays from step 0 with the override, not from step 5
with ReplaySession.from_trace("forked.sftrace", mode="frozen") as replay:
    result = your_agent_entrypoint()
```

⚠️ **Important**: The replay starts from step 0 of the forked trace, not from the
fork point. The forked trace includes events 0-5, so replay will substitute all
those responses including the override at step 5.

## Validation

Forked traces pass strict structural validation:

```bash
$ stepfork validate forked.sftrace --strict

✓ Manifest valid
✓ Event structure valid
✓ Event IDs unique
✓ Parent references valid
✓ Run IDs consistent
✓ Event count matches

Validation passed.
```

This ensures forked traces are well-formed and can be used with all Stepfork
tools (inspect, diff, replay, export, minimize).

## Error Handling

### Common Errors

**Invalid fork point:**
```python
ForkError: step 999 not found in trace
```

**Invalid override target:**
```python
ForkError: invalid overrides: override targets step 1 (tool_call);
only tool_result and llm_response can be overridden
```

**Missing fork point:**
```python
ValueError: fork point requires either event_id or step
```

**Conflicting overrides:**
```python
ForkError: duplicate override for step 5
```

### Best Practices

1. **Inspect first**: Use `stepfork inspect` to understand event types and steps
2. **Validate overrides**: Check that target events are result/response types
3. **Test incrementally**: Fork at different points to isolate issues
4. **Preserve provenance**: Always specify `parent_trace_path` for traceability
5. **Verify results**: Use `stepfork validate` on forked traces

## See Also

- [Failure Minimization](minimize.md) - Automatically find minimal reproducing traces
- [Replay](replay.md) - Execute agents with frozen dependencies
- [Behavioral Diff](diff.md) - Compare traces to find differences
- [CLI Reference](cli.md) - Complete CLI documentation
