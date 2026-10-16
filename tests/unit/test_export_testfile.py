from __future__ import annotations

from pathlib import Path

import pytest

from stepfork.export.generator import generate_pytest_source
from stepfork.export.testfile import ExportedTestError, read_exported_test

EXPECTATION = {"status": "refunded", "amount": 42.5}


def _write(tmp_path: Path, source: str, name: str = "test_run_regression.py") -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def _generated(tmp_path: Path, *, depth: int = 0, **kwargs: object) -> Path:
    bundle = tmp_path
    for index in range(depth):
        bundle = bundle / f"level{index}"
    bundle = bundle / "refund.sftrace"
    output = tmp_path / "test_refund_regression.py"
    source = generate_pytest_source(
        trace_path=bundle,
        output_path=output,
        entrypoint="pkg.mod:run",
        expectation=EXPECTATION,
        **kwargs,  # type: ignore[arg-type]
    )
    return _write(tmp_path, source)


def test_reads_the_literals_the_generator_writes(tmp_path: Path) -> None:
    test = _generated(tmp_path)
    exported = read_exported_test(test)
    assert exported.path == test
    assert exported.trace_path == tmp_path / "refund.sftrace"
    assert exported.expectation == EXPECTATION
    assert exported.has_expectation is True
    assert exported.trajectory_expectation is None


def test_reads_the_absolute_path_form(tmp_path: Path) -> None:
    """A bundle too far up the tree is written as an absolute Path(...)."""
    test = _generated(tmp_path, depth=5)
    assert "Path('/" in test.read_text(encoding="utf-8")
    exported = read_exported_test(test)
    assert exported.trace_path == (
        tmp_path / "level0/level1/level2/level3/level4/refund.sftrace"
    )


def test_reads_a_reviewed_trajectory_expectation(tmp_path: Path) -> None:
    trajectory = {"called": ["search_flights"], "sequence": ["search_flights"]}
    test = _generated(tmp_path, trajectory_expectation=trajectory)
    assert read_exported_test(test).trajectory_expectation == trajectory


def test_reads_an_export_without_an_expectation(tmp_path: Path) -> None:
    test = _generated(tmp_path, has_expectation=False)
    exported = read_exported_test(test)
    assert exported.has_expectation is False
    assert exported.expectation is None


def test_reading_never_executes_the_file(tmp_path: Path) -> None:
    """The contract is parse-and-read; an exported test must not run here."""
    marker = tmp_path / "executed.marker"
    test = _write(
        tmp_path,
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        "TRACE_PATH = Path('/nowhere/run.sftrace')\n"
        "EXPECTATION = {'ok': True}\n",
    )
    exported = read_exported_test(test)
    assert exported.expectation == {"ok": True}
    assert not marker.exists()


def test_refuses_a_non_literal_expectation(tmp_path: Path) -> None:
    test = _write(
        tmp_path,
        "TRACE_PATH = Path('/nowhere/run.sftrace')\nEXPECTATION = dict(status='ok')\n",
    )
    with pytest.raises(ExportedTestError, match="not a literal"):
        read_exported_test(test)


def test_refuses_an_unsupported_bundle_expression(tmp_path: Path) -> None:
    test = _write(
        tmp_path,
        "TRACE_PATH = Path(__file__).resolve().parent / TRACES[0]\n"
        "EXPECTATION = {'ok': True}\n",
    )
    with pytest.raises(ExportedTestError, match="--bundle"):
        read_exported_test(test)


def test_a_file_without_trace_path_reports_none(tmp_path: Path) -> None:
    test = _write(tmp_path, "EXPECTATION = {'ok': True}\n")
    exported = read_exported_test(test)
    assert exported.trace_path is None
    assert exported.has_expectation is True


def test_refuses_a_non_boolean_has_expectation(tmp_path: Path) -> None:
    test = _write(
        tmp_path,
        "TRACE_PATH = Path('/nowhere/run.sftrace')\nEXPECTATION = {'ok': True}\n"
        "HAS_EXPECTATION = 'yes'\n",
    )
    with pytest.raises(ExportedTestError, match="HAS_EXPECTATION"):
        read_exported_test(test)


def test_the_last_assignment_wins(tmp_path: Path) -> None:
    """The module's value is its final one, so reading agrees with running."""
    test = _write(
        tmp_path,
        "TRACE_PATH = Path('/first/run.sftrace')\n"
        "TRACE_PATH = Path('/second/run.sftrace')\n"
        "EXPECTATION = {'ok': True}\n",
    )
    assert read_exported_test(test).trace_path == Path("/second/run.sftrace")


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ExportedTestError, match="cannot read"):
        read_exported_test(tmp_path / "absent.py")


def test_an_unparsable_file_is_reported(tmp_path: Path) -> None:
    test = _write(tmp_path, "EXPECTATION = {'ok':\n")
    with pytest.raises(ExportedTestError, match="does not parse"):
        read_exported_test(test)
