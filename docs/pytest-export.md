# pytest Export

`stepfork export` turns a trace into an executable pytest regression test that
replays a trusted entrypoint and asserts the expected behavior.

## The key idea: the recorded failure is not the expectation

A trace records what the agent *did*, including the bug. If you generate a test
that asserts the recorded output, it passes against the very code that produced
the recording, which is not a regression test.

The regression test needs a separate **expected outcome**: the behavior you want
after the bug is fixed. Pass it with `--expect-output`.

```bash
stepfork export failure.sftrace \
  --pytest \
  --entrypoint examples.booking_agent:run_agent \
  --expect-output expected.json \
  --output test_booking_regression.py \
  --overwrite
```

- Fails against the buggy implementation that produced the recording.
- Passes against the corrected implementation.

Without `--expect-output`, Stepfork warns that the test asserts the recorded run
output and that it will pass against the code that produced the recording. It is
useful for replay-fidelity checks, not for regression testing.

Review the desired JSON output before export. One practical workflow is to copy
the recorded result into a separate `expected.json`, edit the fields that define
the fix, review the file in code review, then pass it with `--expect-output`.
Do not treat the recorded result or the first output from changed code as an
approved oracle automatically. A future assisted workflow could prepare a
draft expected-output file for review, but it should require an explicit user
approval before exporting a regression assertion.

## Options

- `--pytest` / `--no-pytest`: only `pytest` is supported in v0.1.
- `--entrypoint MODULE:FUNCTION`: required; the trusted local callable the test
  executes.
- `--expect-output PATH`: JSON file with the expected outcome.
- `--expect-trajectory PATH`: reviewed JSON file with tool and step assertions.
- `--output PATH`: destination file (default `test_<trace>_regression.py`).
- `--overwrite`: replace an existing destination.
- `--mode`: replay mode used by the generated test (`frozen` recommended).
- `--allow-live-tool NAME`: in `live` mode, embed explicit authorization for
  each named tool that the generated test may execute. Review these names and
  their side effects before running the test.

The trace must pass strict validation before export.

Trajectory expectations use this JSON shape:

```json
{
  "called": ["search_flights"],
  "not_called": ["charge_card"],
  "counts": {"search_flights": 1},
  "arguments": [{"name": "search_flights", "value": {"city": "Paris"}, "occurrence": 1}],
  "order": [["search_flights", "book_flight"]],
  "sequence": ["search_flights", "book_flight"],
  "strict_sequence": true,
  "max_steps": 4
}
```

`sequence` is exact by default. Set `strict_sequence` to `false` for an ordered
subsequence. An argument expectation without `occurrence` matches any call to
that tool; occurrence numbers start at 1. Failed recorded tool calls count.
`max_steps` counts instrumented tool and LLM calls. These assertions inspect
calls made by the replayed entrypoint, and reject incomplete or divergent
replay. Review this JSON separately from the recorded trace; a trace is not an
approved expectation. The export command validates the shape before writing.

Running the generated file requires `pytest`, an installed `stepfork` package,
an importable trusted entrypoint and its dependencies, and the referenced
`.sftrace` directory. The generated test refers to the bundle by path; it does
not embed or copy the fixture. If moving the test to another project or CI,
copy the bundle with it and verify `TRACE_PATH` resolves there. These are test
environment requirements; `pytest` is not a Stepfork runtime dependency.

## Is the expectation still anchored? (`stepfork score`)

An expectation can be two different things and pass the same green run: the
claim you meant to assert, or a copy of the run you wrote it from. `stepfork
score` separates them, and it does it by derivation rather than by execution.

```bash
stepfork score test_booking_regression.py
stepfork score test_booking_regression.py --expect-output expected.json --json
```

Two values are already at hand when the export is prepared, and both are read
back rather than re-derived:

- the **expectation** the generated test asserts;
- the **recorded run's output** (`RunEnd.output` in the bundle), which is the
  value the buggy code that produced the recording actually returned.

The claim-drawn row is that second value verbatim: the run's own output is the
most plausible output of a variant implementation, because an implementation did
produce it. It is the row that discriminates. A claim-pinned expectation rejects
it -- the run it came from is the bug, so the expectation must fail against it.
A run-pinned expectation accepts it, and that acceptance is the finding: the
granularity of the comparison means the generated test would pass against the
code that produced the recording.

The remaining rows are **controls**, not discriminators: perturbations of the
expectation's own leaves (an integer moved by one, a string suffixed, a value
replaced by `null`). A run-pinned expectation rejects those too, so they cannot
separate the two readings by themselves; what they do is make the contrast
legible, because a battery in which every control is killed and the claim-drawn
row survives reads differently from one in which everything is killed.

`score` also reports **pin coverage**: which leaves of the expectation carry a
value the run did not produce (live pins), and which merely reproduce the run.
A leaf that reproduces the run is not a defect -- a fix does not change every
field -- but it is not evidence of anchoring either, which is why it is reported
rather than counted.

Exit codes: `0` when the expectation is claim-pinned, `1` when it is run-pinned,
`2` when the question cannot be answered.

Three limits are worth stating plainly.

- **No recorded run, no claim-drawn row.** A trace whose run did not complete
  carries no output, so there is nothing to draw the discriminating row from.
  The battery is then the controls alone and the verdict is `undetermined` -- a
  reading, not a failure, and the honest one for a battery that cannot separate
  the two.
- **The bundle must be there.** `score` reads the recorded run from the bundle
  the test names, so it has the same requirement the test has. Pass `--bundle`
  to point at one elsewhere.
- **Nothing is executed.** Neither the test file nor the entrypoint runs: the
  file is parsed, the literals are read, and each row is scored with the same
  canonical comparison the generated test uses.

## Generated test

The generated file is produced from a fixed, reviewed template. Trace payloads
are never interpolated into executable code: only sanitized JSON literals,
relative paths, and the entrypoint specification are embedded. No `eval` or
`exec` is used on trace data.

```python
from stepfork.export.runtime import run_regression_case

TRACE_PATH = ...
ENTRYPOINT = "examples.booking_agent:run_agent"
EXPECTATION = {"destination": "Lisbon", "selected_flight": "B", ...}
HAS_EXPECTATION = True
TRAJECTORY_EXPECTATION = {"called": ["search_flights"]}


def test_stepfork_regression_failure() -> None:
    run_regression_case(...)
```

`run_regression_case` loads the frozen trace, resolves the entrypoint, replays
it, verifies that every recorded dependency call was consumed, and compares the
returned value to the expectation. Any divergence raises an actionable
`AssertionError`, for example:

```
AssertionError: behavior mismatch at 'selected_flight': expected "B", got "A"
```

## End-to-end example

`examples/booking_agent/demo.py` runs the full workflow (record, validate,
inspect, frozen replay, diff, export, pytest) and proves that the generated test
fails on the buggy agent and passes on the fixed one:

```bash
uv run python examples/booking_agent/demo.py
```
