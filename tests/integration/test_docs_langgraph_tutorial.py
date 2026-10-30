"""Execute every command in ``docs/tutorials/langgraph-triage.md``.

The tutorial is a sequence, not a single snippet: write the agent, record a run,
validate the bundle, replay it, export a regression test and run that test on
the buggy and on the fixed entrypoint. Each of those commands is run here in a
clean subprocess, so a documented command that stops working -- an option
renamed, a flag that no longer exists, a bundle that no longer validates --
fails this test instead of failing a reader.

The two code blocks the page ships (the agent and, in step 7, the correction)
are extracted from the Markdown and concatenated, so the page cannot drift from
what actually runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TUTORIAL = REPO_ROOT / "docs" / "tutorials" / "langgraph-triage.md"

AGENT_START = "<!-- langgraph-tutorial:start -->"
AGENT_END = "<!-- langgraph-tutorial:end -->"
FIX_START = "<!-- langgraph-tutorial-fix:start -->"
FIX_END = "<!-- langgraph-tutorial-fix:end -->"

BUNDLE = "triage.sftrace"
BUGGY_TEST = "test_triage.py"
FIXED_TEST = "test_triage_fixed.py"


def _extract(start_marker: str, end_marker: str) -> str:
    text = TUTORIAL.read_text(encoding="utf-8")
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker)
    match = re.search(r"```python\n(.*?)```", text[start:end], re.DOTALL)
    assert match is not None, (
        f"no python block between {start_marker!r} and {end_marker!r}"
    )
    return match.group(1)


def _clean_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("STEPFORK_")
    }
    env.pop("PYTHONPATH", None)
    return env


def _run(
    args: list[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env=env, text=True, capture_output=True, check=False
    )


def _stepfork(*args: str) -> list[str]:
    return [sys.executable, "-m", "stepfork", *args]


def test_tutorial_records_replays_and_regresses(tmp_path: Path) -> None:
    agent = tmp_path / "triage_agent.py"
    agent.write_text(_extract(AGENT_START, AGENT_END), encoding="utf-8")
    env = _clean_env()

    # Step 2: recording a run writes a bundle and the buggy decision.
    recorded = _run([sys.executable, str(agent)], tmp_path, env)
    assert recorded.returncode == 0, recorded.stderr
    assert "'decision': 'queue'" in recorded.stdout, recorded.stdout

    bundle = tmp_path / BUNDLE
    assert bundle.is_dir()
    event_types = [
        json.loads(line)["type"]
        for line in (bundle / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "llm_request" in event_types, event_types
    assert "tool_call" in event_types, event_types

    # Step 3: the bundle validates and its payload digests verify.
    validated = _run(_stepfork("validate", BUNDLE, "--verify-integrity"), tmp_path, env)
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "Validation passed." in validated.stdout

    inspected = _run(_stepfork("inspect", BUNDLE), tmp_path, env)
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    assert "VERIFIED" in inspected.stdout

    # Step 4: frozen replay substitutes every recorded boundary.
    replayed = _run(
        _stepfork("replay", BUNDLE, "--entrypoint", "triage_agent:run_agent"),
        tmp_path,
        env,
    )
    assert replayed.returncode == 0, replayed.stdout + replayed.stderr
    assert "(substituted)" in replayed.stdout, replayed.stdout
    assert "Dependency matching: COMPLETE" in replayed.stdout
    assert "Recorded behavior: MATCHED" in replayed.stdout

    # Step 5: the expectation the reader writes by hand.
    expectation = tmp_path / "expected.json"
    expectation.write_text('{"decision": "escalate"}', encoding="utf-8")

    # Step 6: the exported test fails on the buggy entrypoint, for the bug.
    exported = _run(
        _stepfork(
            "export",
            BUNDLE,
            "--pytest",
            "--entrypoint",
            "triage_agent:run_agent",
            "--expect-output",
            "expected.json",
            "--output",
            BUGGY_TEST,
            "--overwrite",
        ),
        tmp_path,
        env,
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr

    buggy = _run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", BUGGY_TEST],
        tmp_path,
        env,
    )
    assert buggy.returncode != 0, buggy.stdout
    assert "behavior mismatch" in buggy.stdout, buggy.stdout
    assert '"queue"' in buggy.stdout, buggy.stdout

    # Step 7: the same expectation passes on the corrected entrypoint.
    fix = _extract(FIX_START, FIX_END)
    agent.write_text(_extract(AGENT_START, AGENT_END) + "\n\n" + fix, encoding="utf-8")
    reexported = _run(
        _stepfork(
            "export",
            BUNDLE,
            "--pytest",
            "--entrypoint",
            "triage_agent:run_agent_fixed",
            "--expect-output",
            "expected.json",
            "--output",
            FIXED_TEST,
            "--overwrite",
        ),
        tmp_path,
        env,
    )
    assert reexported.returncode == 0, reexported.stdout + reexported.stderr

    fixed = _run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", FIXED_TEST],
        tmp_path,
        env,
    )
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr


def test_tutorial_links_its_references() -> None:
    text = TUTORIAL.read_text(encoding="utf-8")
    # The page must point at the guide and the example rather than restate them.
    assert "]( ../langgraph.md)" not in text
    assert "](../langgraph.md)" in text
    assert "examples/langgraph_agent" in text
    assert 'pip install "stepfork[langgraph]"' in text
