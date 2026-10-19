"""Every place the worker ends a task badly names the failure code of the end.

The code is a required argument of `TaskFailure`, and a failure `TaskOutcome` or `RunOutcome` is
only complete with one. This test reads the worker's source and fails when a call site leaves
the code out, so a new failure path cannot fall back to ``internal`` by being forgotten: only a
failure nobody expected may be recorded as ``internal``, and that is the code its call site gives.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "src" / "lakana_worker"
_FILES = sorted(_SOURCE.glob("*.py"))


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    """Find the calls of a name in a module.

    Parameters
    ----------
    tree
        The parsed module.
    name
        The called name, plain or as an attribute.

    Returns
    -------
    list of ast.Call
        The calls, in source order.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    """Return the value of a keyword argument.

    Parameters
    ----------
    call
        The call.
    name
        The keyword.

    Returns
    -------
    ast.expr or None
        The value, or `None` when the call does not pass it.
    """
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_none(node: ast.expr | None) -> bool:
    """Say whether an expression is the literal ``None``.

    Parameters
    ----------
    node
        The expression.

    Returns
    -------
    bool
        `True` for ``None``.
    """
    return isinstance(node, ast.Constant) and node.value is None


def _is_release_or_success(node: ast.expr) -> bool:
    """Say whether a first argument names a success or a release, which fail nothing.

    Parameters
    ----------
    node
        The `kind` argument of a `TaskOutcome`.

    Returns
    -------
    bool
        `True` for ``None`` and ``FailureKind.RELEASED``.
    """
    return _is_none(node) or (isinstance(node, ast.Attribute) and node.attr == "RELEASED")


def _where(path: Path, call: ast.Call) -> str:
    """Name a call site for a failure message.

    Parameters
    ----------
    path
        The module.
    call
        The call.

    Returns
    -------
    str
        ``file:line``.
    """
    return f"{path.name}:{call.lineno}"


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_every_task_failure_names_its_code(path: Path) -> None:
    """`TaskFailure(kind, message, code, ...)`: the code is there, and only a release has none.

    Parameters
    ----------
    path
        A module of the worker.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing = []
    for call in _calls(tree, "TaskFailure"):
        code = call.args[2] if len(call.args) > 2 else _keyword(call, "code")
        if code is None or _is_none(code):
            missing.append(_where(path, call))
    assert missing == []


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_every_failure_outcome_names_its_code(path: Path) -> None:
    """A `TaskOutcome` (or a Python attempt's `ended`) that fails passes `failure_code`.

    Parameters
    ----------
    path
        A module of the worker.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing = [
        _where(path, call)
        for call in (*_calls(tree, "TaskOutcome"), *_calls(tree, "ended"))
        if call.args
        and not _is_release_or_success(call.args[0])
        and _keyword(call, "failure_code") is None
    ]
    assert missing == []


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_every_failed_process_outcome_names_its_code(path: Path) -> None:
    """A `RunOutcome(succeeded=False, ...)` passes `failure_code`.

    Parameters
    ----------
    path
        A module of the worker.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing = []
    for call in _calls(tree, "RunOutcome"):
        succeeded = _keyword(call, "succeeded")
        if (
            isinstance(succeeded, ast.Constant)
            and succeeded.value is False
            and _keyword(call, "failure_code") is None
        ):
            missing.append(_where(path, call))
    assert missing == []


def test_the_scan_sees_the_sites() -> None:
    """The scan finds the call sites it guards, so an empty result is not a broken scan."""
    counts = {"TaskFailure": 0, "TaskOutcome": 0, "RunOutcome": 0}
    for path in _FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in counts:
            counts[name] += len(_calls(tree, name))
    assert counts["TaskFailure"] > 30
    assert counts["TaskOutcome"] > 15
    assert counts["RunOutcome"] > 10
