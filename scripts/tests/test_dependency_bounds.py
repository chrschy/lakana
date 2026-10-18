"""The dependencies whose API Lakana uses deeply must not float onto a release nobody tested."""

import re
import tomllib
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parents[2] / "packages"

DEEPLY_USED = [
    ("lakana-core", "sqlglot"),
    ("lakana-core", "pyiceberg"),
    ("lakana-worker", "duckdb"),
]


def _requirements(package: str) -> list[str]:
    """Collect every runtime requirement a package declares, including its build hook's.

    Parameters
    ----------
    package
        Directory name under ``packages/``.

    Returns
    -------
    list of str
        The requirement strings.
    """
    data = tomllib.loads((PACKAGES / package / "pyproject.toml").read_text())
    hook = data.get("tool", {}).get("hatch", {}).get("metadata", {}).get("hooks", {})
    return [
        *data["project"].get("dependencies", []),
        *hook.get("custom", {}).get("dependencies", []),
    ]


@pytest.mark.parametrize(("package", "dependency"), DEEPLY_USED)
def test_a_deeply_used_dependency_has_an_upper_bound(package: str, dependency: str) -> None:
    """`sqlglot`, `pyiceberg` and `duckdb` carry an upper bound, raised on purpose.

    A fresh ``pip install`` ignores the lock, so an unbounded requirement takes the newest
    release on the day. The bound is raised in its own pull request, after reading the upstream
    changelog and running the suite.
    """
    (requirement,) = (r for r in _requirements(package) if re.match(rf"{dependency}\b", r))
    assert re.search(r"<=?\s*\d", requirement), (
        f"{package} must give {dependency} an upper bound (found {requirement!r}): a fresh install "
        "ignores the lock and would take a release Lakana was not tested on. Raise the bound in "
        "its own pull request, after reading the upstream changelog and running the suite."
    )
