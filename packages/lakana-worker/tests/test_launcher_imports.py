"""The launcher holds the Docker socket: it loads nothing but the standard library and itself."""

from __future__ import annotations

import ast
import subprocess
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


def test_the_launcher_starts_without_site_packages_and_loads_only_the_standard_library() -> None:
    """`python -I -S` has no `.pth` files and no installed packages: the launcher still imports."""
    source = str(_PACKAGE.parent.parent)
    code = (
        "import sys, sysconfig\n"
        f"sys.path.insert(0, {source!r})\n"
        "import lakana_worker.launcher.__main__, lakana_worker.launcher.server\n"
        "stdlib = sysconfig.get_paths()['stdlib']\n"
        "odd = sorted(\n"
        "    name for name, module in sys.modules.items()\n"
        "    if getattr(getattr(module, '__spec__', None), 'origin', None)\n"
        "    and module.__spec__.origin not in ('built-in', 'frozen')\n"
        "    and not module.__spec__.origin.startswith(stdlib)\n"
        f"    and not module.__spec__.origin.startswith({source!r})\n"
        ")\n"
        "print('ODD', odd)\n"
    )

    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code], check=False, capture_output=True, text=True
    )

    assert done.returncode == 0, done.stderr
    assert "ODD []" in done.stdout, done.stdout
