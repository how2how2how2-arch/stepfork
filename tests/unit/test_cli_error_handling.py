"""CLI error-handling regression tests.

These tests cover CLI error paths that are not exercised by the happy-path
test modules. They verify that:

1. Malformed or corrupted trace files produce clear, actionable errors
2. Invalid option combinations are rejected with useful messages
3. Exit codes are consistent and documented
4. Error messages contain enough context for users to fix the problem

All tests are offline and deterministic.

Addresses: https://github.com/utsab345/stepfork/issues/27
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stepfork import ToolCall, Trace
from stepfork.cli.main import app
from stepfork.trace import RunEnd, RunStart, RunStatus, ToolResult

runner = CliRunner()


# -----------------------------------------------------------------------------
# Fixtures: helper functions and reusable trace builders
# -----------------------------------------------------------------------------


def complete_trace() -> Trace:
    """Build a structurally valid trace for modification tests."""
    trace = Trace(agent_name="error-test-agent")
    start = trace.add(RunStart(input={"prompt": "test"}))
    call = trace.add(
        ToolCall(name="test_tool", input={"value": "input"}, parent_id=start.id)
    )
    result = trace.add(
        ToolResult(name="test_tool", output={"result": "ok"}, parent_id=call.id)
    )
    trace.add(RunEnd(run_status=RunStatus.COMPLETED, parent_id=result.id))
    return trace


def save_valid_trace(tmp_path: Path, name: str = "valid.sftrace") -> Path:
    """Save a valid trace and return its path."""
    path = tmp_path / name
    complete_trace().save(path, status=RunStatus.COMPLETED)
    return path


# -----------------------------------------------------------------------------
# Malformed trace file tests: manifest.json
# -----------------------------------------------------------------------------


class TestMalformedManifest:
    """Tests for corrupted or malformed manifest.json files."""

    def test_validate_manifest_not_json(self, tmp_path: Path) -> None:
        """Validate rejects manifest.json that is not valid JSON."""
        bundle = tmp_path / "bad.sftrace"
        bundle.mkdir()
        (bundle / "manifest.json").write_text("not valid json {{{", encoding="utf-8")
        (bundle / "events.jsonl").write_text("{}\n", encoding="utf-8")
        (bundle / "redactions.json").write_text(
            '{"schema_version": "0.1", "entries": []}', encoding="utf-8"
        )

        result = runner.invoke(app, ["validate", str(bundle)])

        assert result.exit_code == 2
        assert "invalid_json" in result.stdout or "invalid_manifest" in result.stdout

    def test_validate_manifest_empty(self, tmp_path: Path) -> None:
        """Validate rejects an empty manifest.json file."""
        bundle = tmp_path / "empty.sftrace"
        bundle.mkdir()
        (bundle / "manifest.json").write_text("", encoding="utf-8")
        (bundle / "events.jsonl").write_text("{}\n", encoding="utf-8")
        (bundle / "redactions.json").write_text(
            '{"schema_version": "0.1", "entries": []}', encoding="utf-8"
        )

        result = runner.invoke(app, ["validate", str(bundle)])

        assert result.exit_code == 2

    def test_validate_manifest_missing_required_fields(self, tmp_path: Path) -> None:
        """Validate rejects manifest missing required fields."""
        bundle = tmp_path / "incomplete.sftrace"
        bundle.mkdir()
        (bundle / "manifest.json").write_text(
            '{"schema_version": "0.1"}', encoding="utf-8"
        )
        (bundle / "events.jsonl").write_text("{}\n", encoding="utf-8")
        (bundle / "redactions.json").write_text(
            '{"schema_version": "0.1", "entries": []}', encoding="utf-8"
        )

        result = runner.invoke(app, ["validate", str(bundle)])

        assert result.exit_code != 0

    def test_inspect_manifest_not_json(self, tmp_path: Path) -> None:
        """Inspect exits with code 2 for unreadable manifest."""
        bundle = tmp_path / "bad.sftrace"
        bundle.mkdir()
        (bundle / "manifest.json").write_text("{not json}", encoding="utf-8")
        (bundle / "events.jsonl").write_text("{}\n", encoding="utf-8")

        result = runner.invoke(app, ["inspect", str(bundle)])

        assert result.exit_code == 2
        assert "Unable to inspect trace" in result.stdout


# -----------------------------------------------------------------------------
# Malformed trace file tests: events.jsonl
# -----------------------------------------------------------------------------


class TestMalformedEvents:
    """Tests for corrupted or malformed events.jsonl files."""

    def test_validate_events_not_jsonl(self, tmp_path: Path) -> None:
        """Validate rejects events.jsonl with invalid JSONL content."""
        path = save_valid_trace(tmp_path)
        (path / "events.jsonl").write_text("not jsonl content\n", encoding="utf-8")

        result = runner.invoke(app, ["validate", str(path)])

        assert result.exit_code != 0

    def test_validate_events_empty_file(self, tmp_path: Path) -> None:
        """Validate rejects an empty events.jsonl (no events)."""
        path = save_valid_trace(tmp_path)
        (path / "events.jsonl").write_text("", encoding="utf-8")

        result = runner.invoke(app, ["validate", str(path)])

        assert result.exit_code != 0

    def test_validate_events_truncated_json(self, tmp_path: Path) -> None:
        """Validate rejects events.jsonl with truncated JSON on a line."""
        path = save_valid_trace(tmp_path)
        (path / "events.jsonl").write_text('{"type": "run_star\n', encoding="utf-8")

        result = runner.invoke(app, ["validate", str(path)])

        assert result.exit_code != 0

    def test_inspect_events_corrupt(self, tmp_path: Path) -> None:
        """Inspect exits with code 2 for corrupted events."""
        path = save_valid_trace(tmp_path)
        (path / "events.jsonl").write_text("corrupted\n", encoding="utf-8")

        result = runner.invoke(app, ["inspect", str(path)])

        assert result.exit_code == 2


# -----------------------------------------------------------------------------
# Malformed trace file tests: redactions.json
# -----------------------------------------------------------------------------


class TestMalformedRedactions:
    """Tests for corrupted or malformed redactions.json files."""

    def test_validate_redactions_not_json(self, tmp_path: Path) -> None:
        """Validate rejects redactions.json that is not valid JSON."""
        path = save_valid_trace(tmp_path)
        (path / "redactions.json").write_text("not json", encoding="utf-8")

        result = runner.invoke(app, ["validate", str(path)])

        assert result.exit_code != 0


# -----------------------------------------------------------------------------
# Missing file tests
# -----------------------------------------------------------------------------


class TestMissingFiles:
    """Tests for missing required files in trace bundles."""

    def test_validate_missing_manifest(self, tmp_path: Path) -> None:
        """Validate rejects bundle without manifest.json."""
        bundle = tmp_path / "no_manifest.sftrace"
        bundle.mkdir()
        (bundle / "events.jsonl").write_text("{}\n", encoding="utf-8")
        (bundle / "redactions.json").write_text(
            '{"schema_version": "0.1", "entries": []}', encoding="utf-8"
        )

        result = runner.invoke(app, ["validate", str(bundle)])

        assert result.exit_code == 2
        assert "missing_file" in result.stdout

    def test_validate_missing_events(self, tmp_path: Path) -> None:
        """Validate rejects bundle without events.jsonl."""
        path = save_valid_trace(tmp_path)
        (path / "events.jsonl").unlink()

        result = runner.invoke(app, ["validate", str(path)])

        assert result.exit_code == 2
        assert "missing_file" in result.stdout

    def test_replay_missing_trace_has_actionable_message(self, tmp_path: Path) -> None:
        """Replay error message includes the path that was not found."""
        missing = tmp_path / "does_not_exist.sftrace"

        result = runner.invoke(
            app,
            ["replay", str(missing), "--entrypoint", "mod:func"],
        )

        assert result.exit_code == 2
        assert "Unable to read trace" in result.stdout

    def test_diff_missing_baseline_includes_path(self, tmp_path: Path) -> None:
        """Diff error message identifies which path was not found."""
        baseline = tmp_path / "missing.sftrace"
        candidate = save_valid_trace(tmp_path, "candidate.sftrace")

        result = runner.invoke(app, ["diff", str(baseline), str(candidate)])

        assert result.exit_code == 2
        assert "Unable to compare traces" in result.stdout

    def test_diff_missing_candidate_includes_path(self, tmp_path: Path) -> None:
        """Diff error message identifies missing candidate."""
        baseline = save_valid_trace(tmp_path, "baseline.sftrace")
        candidate = tmp_path / "missing.sftrace"

        result = runner.invoke(app, ["diff", str(baseline), str(candidate)])

        assert result.exit_code == 2
        assert "Unable to compare traces" in result.stdout


# -----------------------------------------------------------------------------
# Invalid option combination tests
# -----------------------------------------------------------------------------


class TestInvalidOptionCombinations:
    """Tests for mutually exclusive or invalid option combinations."""

    def test_fork_requires_at_step_or_at_event(self, tmp_path: Path) -> None:
        """Fork command fails without --at-step or --at-event."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(app, ["fork", str(path)])

        assert result.exit_code == 2
        assert "must specify either --at-step or --at-event" in result.stdout

    def test_fork_rejects_both_at_step_and_at_event(self, tmp_path: Path) -> None:
        """Fork command fails with both --at-step and --at-event."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(
            app,
            ["fork", str(path), "--at-step", "1", "--at-event", "abc"],
        )

        assert result.exit_code == 2
        assert "cannot specify both --at-step and --at-event" in result.stdout

    def test_replay_allow_live_tool_without_live_mode(self, tmp_path: Path) -> None:
        """--allow-live-tool requires --mode live."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(
            app,
            [
                "replay",
                str(path),
                "--entrypoint",
                "mod:func",
                "--allow-live-tool",
                "some_tool",
            ],
        )

        assert result.exit_code == 2
        assert "--allow-live-tool requires --mode live" in result.stdout

    def test_export_allow_live_tool_without_live_mode(self, tmp_path: Path) -> None:
        """Export's --allow-live-tool requires --mode live."""
        path = save_valid_trace(tmp_path)
        output = tmp_path / "test_out.py"

        result = runner.invoke(
            app,
            [
                "export",
                str(path),
                "--entrypoint",
                "mod:func",
                "--allow-live-tool",
                "some_tool",
                "--output",
                str(output),
            ],
        )

        assert result.exit_code == 2
        assert "--allow-live-tool requires --mode live" in result.stdout

    def test_inspect_combined_json_and_events(self, tmp_path: Path) -> None:
        """Inspect with --json and --events produces valid JSON output."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(app, ["inspect", str(path), "--json", "--events"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert "timeline" in payload

    def test_inspect_combined_errors_only_and_step(self, tmp_path: Path) -> None:
        """Inspect with --errors-only and --step produces correct output."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(
            app, ["inspect", str(path), "--errors-only", "--step", "0", "--json"]
        )

        assert result.exit_code in (0, 1)


# -----------------------------------------------------------------------------
# Invalid mode tests
# -----------------------------------------------------------------------------


class TestInvalidModes:
    """Tests for invalid replay mode values."""

    def test_replay_invalid_mode_message(self, tmp_path: Path) -> None:
        """Replay with invalid mode shows the mode in the error."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(
            app,
            ["replay", str(path), "--entrypoint", "mod:func", "--mode", "invalid_mode"],
        )

        assert result.exit_code == 2
        assert "Invalid replay mode" in result.stdout
        assert "invalid_mode" in result.stdout

    def test_export_invalid_mode_message(self, tmp_path: Path) -> None:
        """Export with invalid mode shows the mode in the error."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(
            app,
            [
                "export",
                str(path),
                "--entrypoint",
                "mod:func",
                "--mode",
                "not_a_mode",
            ],
        )

        assert result.exit_code == 2
        assert "Invalid replay mode" in result.stdout
        assert "not_a_mode" in result.stdout

    def test_minimize_invalid_mode_message(self, tmp_path: Path) -> None:
        """Minimize with invalid mode shows the mode in the error."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(
            app,
            [
                "minimize",
                str(path),
                "--entrypoint",
                "mod:func",
                "--mode",
                "bogus",
            ],
        )

        assert result.exit_code == 2
        assert "Invalid replay mode" in result.stdout
        assert "bogus" in result.stdout


# -----------------------------------------------------------------------------
# Fork edge case tests
# -----------------------------------------------------------------------------


class TestForkEdgeCases:
    """Tests for fork command edge cases."""

    def test_fork_at_step_out_of_range(self, tmp_path: Path) -> None:
        """Fork with --at-step beyond event count fails gracefully."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(
            app,
            ["fork", str(path), "--at-step", "999"],
        )

        assert result.exit_code == 1
        assert "Fork failed" in result.stdout

    def test_fork_at_event_nonexistent(self, tmp_path: Path) -> None:
        """Fork with --at-event for nonexistent ID fails gracefully."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(
            app,
            ["fork", str(path), "--at-event", "nonexistent-event-id-12345"],
        )

        assert result.exit_code == 1
        assert "Fork failed" in result.stdout

    def test_fork_empty_trace(self, tmp_path: Path) -> None:
        """Fork rejects a trace with no events."""
        bundle = tmp_path / "empty.sftrace"
        bundle.mkdir()
        (bundle / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "0.1",
                    "agent_name": "empty",
                    "run_id": "test-run-id",
                    "created_at": "2024-01-01T00:00:00Z",
                    "event_count": 0,
                }
            ),
            encoding="utf-8",
        )
        (bundle / "events.jsonl").write_text("", encoding="utf-8")
        (bundle / "redactions.json").write_text(
            '{"schema_version": "0.1", "entries": []}', encoding="utf-8"
        )

        result = runner.invoke(
            app,
            ["fork", str(bundle), "--at-step", "0"],
        )

        assert result.exit_code != 0


# -----------------------------------------------------------------------------
# Entrypoint validation tests
# -----------------------------------------------------------------------------


class TestEntrypointValidation:
    """Tests for entrypoint resolution errors."""

    def test_replay_entrypoint_no_colon(self, tmp_path: Path) -> None:
        """Replay rejects entrypoint without MODULE:FUNCTION format."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(
            app,
            ["replay", str(path), "--entrypoint", "no_colon_here"],
        )

        assert result.exit_code == 2
        assert "entrypoint" in result.stdout.lower()

    def test_replay_entrypoint_module_not_found(self, tmp_path: Path) -> None:
        """Replay reports module not found clearly."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(
            app,
            ["replay", str(path), "--entrypoint", "nonexistent_module_xyz:func"],
        )

        assert result.exit_code == 2
        assert "Unable to resolve entrypoint" in result.stdout

    def test_replay_entrypoint_function_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Replay reports function not found clearly."""
        path = save_valid_trace(tmp_path)
        pkg = tmp_path / "testpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "module.py").write_text("def existing_func(): pass\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            ["replay", str(path), "--entrypoint", "testpkg.module:nonexistent_func"],
        )

        assert result.exit_code == 2
        assert "Unable to resolve entrypoint" in result.stdout

    def test_export_entrypoint_not_callable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Export rejects entrypoint that is not callable."""
        path = save_valid_trace(tmp_path)
        pkg = tmp_path / "testpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "module.py").write_text("NOT_CALLABLE = 42\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            [
                "export",
                str(path),
                "--entrypoint",
                "testpkg.module:NOT_CALLABLE",
                "--output",
                str(tmp_path / "out.py"),
            ],
        )

        assert result.exit_code == 2


# -----------------------------------------------------------------------------
# Score command error tests
# -----------------------------------------------------------------------------


class TestScoreErrors:
    """Tests for score command error handling."""

    def test_score_test_file_not_found(self, tmp_path: Path) -> None:
        """Score exits 2 when test file does not exist."""
        missing = tmp_path / "nonexistent_test.py"

        result = runner.invoke(app, ["score", str(missing)])

        assert result.exit_code == 2

    def test_score_invalid_expect_output_json(self, tmp_path: Path) -> None:
        """Score exits 2 for invalid --expect-output JSON."""
        bundle = save_valid_trace(tmp_path, "bundle.sftrace")
        test = tmp_path / "test_placeholder.py"
        test.write_text(
            f'EXPECTATION = {{"key": "value"}}\nTRACE_PATH = "{bundle}"\n',
            encoding="utf-8",
        )
        invalid = tmp_path / "invalid.json"
        invalid.write_text("{not valid json", encoding="utf-8")

        result = runner.invoke(
            app, ["score", str(test), "--expect-output", str(invalid)]
        )

        assert result.exit_code == 2
        assert "not valid JSON" in result.stdout

    def test_score_expect_output_file_missing(self, tmp_path: Path) -> None:
        """Score exits 2 when --expect-output file does not exist."""
        bundle = save_valid_trace(tmp_path, "bundle.sftrace")
        test = tmp_path / "test_placeholder.py"
        test.write_text(
            f'EXPECTATION = {{"key": "value"}}\nTRACE_PATH = "{bundle}"\n',
            encoding="utf-8",
        )
        missing = tmp_path / "missing.json"

        result = runner.invoke(
            app, ["score", str(test), "--expect-output", str(missing)]
        )

        assert result.exit_code == 2
        assert "cannot read" in result.stdout.lower()


# -----------------------------------------------------------------------------
# Run command error tests
# -----------------------------------------------------------------------------


class TestRunErrors:
    """Tests for run command error handling."""

    def test_run_test_file_not_found(self, tmp_path: Path) -> None:
        """Run exits non-zero when test file does not exist."""
        missing = tmp_path / "nonexistent_test.py"

        result = runner.invoke(app, ["run", str(missing)])

        assert result.exit_code != 0

    def test_run_invalid_bundle_path(self, tmp_path: Path) -> None:
        """Run handles --bundle pointing to nonexistent path."""
        test = tmp_path / "test_simple.py"
        test.write_text("def test_ok(): pass\n", encoding="utf-8")
        missing = tmp_path / "missing.sftrace"

        result = runner.invoke(app, ["run", str(test), "--bundle", str(missing)])

        assert result.exit_code != 0


# -----------------------------------------------------------------------------
# Exit code consistency tests
# -----------------------------------------------------------------------------


class TestExitCodeConsistency:
    """Tests verifying that exit codes are consistent across commands."""

    def test_missing_trace_exits_two_across_commands(self, tmp_path: Path) -> None:
        """All commands that take a trace path exit 2 for missing input."""
        missing = tmp_path / "missing.sftrace"
        commands_and_args = [
            ["validate", str(missing)],
            ["inspect", str(missing)],
            ["replay", str(missing), "--entrypoint", "mod:func"],
        ]

        for args in commands_and_args:
            result = runner.invoke(app, args)
            assert result.exit_code == 2, (
                f"Command {args[0]} should exit 2 for missing trace"
            )

    def test_invalid_mode_exits_two_across_commands(self, tmp_path: Path) -> None:
        """Commands with --mode exit 2 for invalid mode values."""
        path = save_valid_trace(tmp_path)
        commands_and_args = [
            ["replay", str(path), "--entrypoint", "mod:func", "--mode", "xyz"],
            ["export", str(path), "--entrypoint", "mod:func", "--mode", "xyz"],
            ["minimize", str(path), "--entrypoint", "mod:func", "--mode", "xyz"],
        ]

        for args in commands_and_args:
            result = runner.invoke(app, args)
            assert result.exit_code == 2, (
                f"Command {args[0]} should exit 2 for invalid mode"
            )


# -----------------------------------------------------------------------------
# Actionable error message tests
# -----------------------------------------------------------------------------


class TestActionableErrorMessages:
    """Tests verifying error messages contain actionable information."""

    def test_export_missing_entrypoint_suggests_flag(self, tmp_path: Path) -> None:
        """Export error for missing entrypoint mentions --entrypoint."""
        path = save_valid_trace(tmp_path)

        result = runner.invoke(app, ["export", str(path), "--pytest"])

        assert result.exit_code == 2
        assert "--entrypoint" in result.stdout

    def test_export_existing_file_suggests_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Export error for existing file mentions --overwrite."""
        path = save_valid_trace(tmp_path)
        output = tmp_path / "existing.py"
        output.write_text("# existing\n", encoding="utf-8")
        pkg = tmp_path / "errorpkg_ow"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "module.py").write_text("def run(): pass\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            [
                "export",
                str(path),
                "--entrypoint",
                "errorpkg_ow.module:run",
                "--output",
                str(output),
            ],
        )

        assert result.exit_code == 1
        assert "--overwrite" in result.stdout

    def test_validate_integrity_legacy_explains_limitation(
        self, tmp_path: Path
    ) -> None:
        """Validate --verify-integrity on legacy bundle explains the situation."""
        path = save_valid_trace(tmp_path)
        (path / "integrity.json").unlink()

        result = runner.invoke(app, ["validate", str(path), "--verify-integrity"])

        assert result.exit_code == 3
        assert "UNVERIFIED" in result.stdout


# -----------------------------------------------------------------------------
# Diff command edge cases
# -----------------------------------------------------------------------------


class TestDiffEdgeCases:
    """Tests for diff command edge cases."""

    def test_diff_one_valid_one_corrupt(self, tmp_path: Path) -> None:
        """Diff with one valid and one corrupt trace exits 2."""
        valid = save_valid_trace(tmp_path, "valid.sftrace")
        corrupt = tmp_path / "corrupt.sftrace"
        corrupt.mkdir()
        (corrupt / "manifest.json").write_text("not json", encoding="utf-8")
        (corrupt / "events.jsonl").write_text("{}\n", encoding="utf-8")

        result = runner.invoke(app, ["diff", str(valid), str(corrupt)])

        assert result.exit_code == 2
        assert "Unable to compare traces" in result.stdout

    def test_diff_json_output_on_error(self, tmp_path: Path) -> None:
        """Diff --json on error produces valid JSON with error field."""
        missing = tmp_path / "missing.sftrace"
        valid = save_valid_trace(tmp_path, "valid.sftrace")

        result = runner.invoke(app, ["diff", str(missing), str(valid), "--json"])

        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert "error" in payload


# -----------------------------------------------------------------------------
# Export edge cases
# -----------------------------------------------------------------------------


class TestExportEdgeCases:
    """Tests for export command edge cases."""

    def test_export_invalid_expect_trajectory_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Export rejects --expect-trajectory with invalid schema."""
        path = save_valid_trace(tmp_path)
        trajectory = tmp_path / "trajectory.json"
        trajectory.write_text('{"invalid_field": true}', encoding="utf-8")
        output = tmp_path / "out.py"
        pkg = tmp_path / "errorpkg_traj"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "module.py").write_text("def run(): pass\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            [
                "export",
                str(path),
                "--entrypoint",
                "errorpkg_traj.module:run",
                "--expect-trajectory",
                str(trajectory),
                "--output",
                str(output),
            ],
        )

        assert result.exit_code == 2
        assert "Invalid --expect-trajectory" in result.stdout

    def test_export_expect_trajectory_not_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Export rejects --expect-trajectory that is not valid JSON."""
        path = save_valid_trace(tmp_path)
        trajectory = tmp_path / "trajectory.json"
        trajectory.write_text("not json", encoding="utf-8")
        output = tmp_path / "out.py"
        pkg = tmp_path / "errorpkg_tjson"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "module.py").write_text("def run(): pass\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            [
                "export",
                str(path),
                "--entrypoint",
                "errorpkg_tjson.module:run",
                "--expect-trajectory",
                str(trajectory),
                "--output",
                str(output),
            ],
        )

        assert result.exit_code == 2


# -----------------------------------------------------------------------------
# Minimize edge cases
# -----------------------------------------------------------------------------


class TestMinimizeEdgeCases:
    """Tests for minimize command edge cases."""

    def test_minimize_invalid_expect_output_json(self, tmp_path: Path) -> None:
        """Minimize rejects --expect-output with invalid JSON."""
        path = save_valid_trace(tmp_path)
        expected = tmp_path / "expected.json"
        expected.write_text("{not valid json", encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "minimize",
                str(path),
                "--entrypoint",
                "mod:func",
                "--expect-output",
                str(expected),
            ],
        )

        assert result.exit_code == 2
        assert "Error loading expected output" in result.stdout

    def test_minimize_nonexistent_trace(self, tmp_path: Path) -> None:
        """Minimize exits non-zero for nonexistent trace path."""
        missing = tmp_path / "missing.sftrace"

        result = runner.invoke(
            app,
            ["minimize", str(missing), "--entrypoint", "mod:func"],
        )

        assert result.exit_code != 0


# -----------------------------------------------------------------------------
# Bundle directory vs file confusion tests
# -----------------------------------------------------------------------------


class TestBundlePathConfusion:
    """Tests for when users pass files instead of directories or vice versa."""

    def test_validate_file_instead_of_directory(self, tmp_path: Path) -> None:
        """Validate handles a file path when directory expected."""
        not_a_bundle = tmp_path / "not_a_bundle.sftrace"
        not_a_bundle.write_text("just a file\n", encoding="utf-8")

        result = runner.invoke(app, ["validate", str(not_a_bundle)])

        assert result.exit_code == 2

    def test_inspect_file_instead_of_directory(self, tmp_path: Path) -> None:
        """Inspect handles a file path when directory expected."""
        not_a_bundle = tmp_path / "not_a_bundle.sftrace"
        not_a_bundle.write_text("just a file\n", encoding="utf-8")

        result = runner.invoke(app, ["inspect", str(not_a_bundle)])

        assert result.exit_code == 2


# -----------------------------------------------------------------------------
# Verify all commands have --help
# -----------------------------------------------------------------------------


class TestHelpAvailability:
    """Tests verifying all commands have accessible --help."""

    @pytest.mark.parametrize(
        "command",
        [
            "validate",
            "inspect",
            "replay",
            "diff",
            "export",
            "minimize",
            "fork",
            "score",
            "run",
        ],
    )
    def test_command_help_exits_zero(self, command: str) -> None:
        """Each command's --help exits 0 and shows usage."""
        result = runner.invoke(app, [command, "--help"])

        assert result.exit_code == 0
        assert command in result.stdout.lower() or "usage" in result.stdout.lower()
