"""Read the literals an exported pytest test carries, without executing it.

``stepfork export`` writes a fixed template whose module-level assignments
name the bundle, the expectation, and whether an expectation is asserted at
all. Reading those back is what lets ``stepfork score`` work on the file the
export just wrote, rather than on a second copy of the expectation that can
drift from it.

Reading is not running. The file is parsed with :mod:`ast`, JSON-shaped values
are read with :func:`ast.literal_eval`, and the bundle path is evaluated by a
whitelist evaluator that understands exactly the two expression shapes the
generator emits -- ``Path(__file__).resolve().parent`` optionally joined with
literal parts, and a bare ``Path("<absolute>")``. Anything else is refused with
a message that names the file and suggests ``--bundle``, because the
alternative to refusing is guessing, and a guess here would score the wrong
bundle silently. Nothing is imported, no code is compiled, and nothing in the
file is executed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from stepfork.trace import JsonValue

READ_NAMES = (
    "TRACE_PATH",
    "EXPECTATION",
    "HAS_EXPECTATION",
    "TRAJECTORY_EXPECTATION",
)


class ExportedTestError(ValueError):
    """Raised when an exported test file cannot be read."""


@dataclass(frozen=True)
class ExportedTest:
    """The literals an exported test carries.

    ``trace_path`` is None when the file assigns no ``TRACE_PATH``; the caller
    may still name a bundle itself, so the absence is reported rather than
    raised here.
    """

    path: Path
    trace_path: Path | None
    expectation: JsonValue
    has_expectation: bool
    trajectory_expectation: JsonValue | None


def read_exported_test(path: Path) -> ExportedTest:
    """Read the literals of an exported regression test.

    Args:
        path: The generated test file.

    Returns:
        The bundle path it names (when it names one), the expectation it
        asserts, whether it asserts one, and any trajectory expectation.

    Raises:
        ExportedTestError: If the file cannot be read, does not parse, assigns a
            value that is not a literal, or names its bundle with an
            expression outside the shapes the generator emits.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExportedTestError(f"cannot read {path}: {exc}") from exc
    try:
        module = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ExportedTestError(f"{path} does not parse: {exc}") from exc

    assigned = _assigned_literals(module)
    expectation = _literal(assigned, "EXPECTATION", path, default=None)
    has_expectation = _literal(assigned, "HAS_EXPECTATION", path, default=True)
    if not isinstance(has_expectation, bool):
        raise ExportedTestError(
            f"{path}: HAS_EXPECTATION is {has_expectation!r}; the export writes a bool"
        )
    trajectory = _literal(assigned, "TRAJECTORY_EXPECTATION", path, default=None)
    node = assigned.get("TRACE_PATH")
    return ExportedTest(
        path=path,
        trace_path=_evaluate_path(node, path) if node is not None else None,
        expectation=expectation,
        has_expectation=has_expectation,
        trajectory_expectation=trajectory,
    )


def _assigned_literals(module: ast.Module) -> dict[str, ast.expr]:
    """Map each read name to the expression of its last module-level assignment."""
    found: dict[str, ast.expr] = {}
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id in READ_NAMES:
                found[target.id] = statement.value
    return found


def _literal(
    assigned: dict[str, ast.expr],
    name: str,
    path: Path,
    *,
    default: JsonValue,
) -> JsonValue:
    """Read one assigned value as a JSON literal."""
    node = assigned.get(name)
    if node is None:
        return default
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        raise ExportedTestError(
            f"{path}:{node.lineno}: {name} is not a literal value "
            f"({type(exc).__name__}); score reads the literals the export wrote"
        ) from exc
    return cast("JsonValue", value)


def _evaluate_path(node: ast.expr, test_path: Path) -> Path:
    """Evaluate a bundle path expression, refusing anything outside the template.

    The accepted shapes are the ones ``stepfork.export.generator._path_expression``
    emits, so a file the generator wrote always reads and a hand-edited
    expression is refused rather than misread.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return Path(node.value)
    if isinstance(node, ast.Name) and node.id == "__file__":
        return test_path
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return _evaluate_path(node.value, test_path).parent
    if isinstance(node, ast.Call):
        function = node.func
        if (
            isinstance(function, ast.Name)
            and function.id == "Path"
            and len(node.args) == 1
        ):
            return Path(_string_argument(node.args[0], test_path))
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "resolve"
            and not node.args
        ):
            return _evaluate_path(function.value, test_path).resolve()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _evaluate_path(node.left, test_path) / _string_argument(
            node.right, test_path
        )
    raise ExportedTestError(
        f"{test_path}:{node.lineno}: cannot read the bundle path from this "
        "expression; pass --bundle to name the .sftrace bundle directly"
    )


def _string_argument(node: ast.expr, test_path: Path) -> str:
    """Read one path part: a string literal, or ``__file__`` inside ``Path(...)``."""
    if isinstance(node, ast.Name) and node.id == "__file__":
        return str(test_path)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise ExportedTestError(
        f"{test_path}:{node.lineno}: a path part is not a string literal; "
        "pass --bundle to name the .sftrace bundle directly"
    )
