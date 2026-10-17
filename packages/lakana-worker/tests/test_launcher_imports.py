"""The launcher holds the Docker socket: it loads nothing but the standard library and itself."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import lakana_worker
import lakana_worker.launcher

_PACKAGE = Path(lakana_worker.launcher.__file__).parent


def _imported_roots(path: Path) -> set[str]:
    """List the top-level modules a file imports, and the launcher modules it names.

    Parameters
    ----------
    path
        A Python file.

    Returns
    -------
    set of str
        ``json``, ``lakana_worker.launcher.requests`` and the like; a relative import is
        written as ``.``.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." if node.level else node.module or "")
    return names


def test_the_launcher_imports_only_the_standard_library() -> None:
    """Every module of the package: standard library, `__future__` or the launcher itself."""
    for path in sorted(_PACKAGE.glob("*.py")):
        for name in _imported_roots(path):
            allowed = (
                name == "."
                or name.split(".")[0] in sys.stdlib_module_names
                or name == "__future__"
                or name == "lakana_worker.launcher"
                or name.startswith("lakana_worker.launcher.")
            )
            assert allowed, f"{path.name} imports {name}"


def test_loading_the_launcher_package_loads_nothing_of_the_worker() -> None:
    """`lakana_worker/__init__.py` runs before the launcher; it must stay without imports."""
    tree = ast.parse(Path(lakana_worker.__file__).read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
