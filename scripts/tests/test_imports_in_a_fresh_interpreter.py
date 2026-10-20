"""Every entry point, and every module, imports in a fresh interpreter.

A package-level import cycle shows only when its first member is imported by itself; the rest
of the suite imports a web application or the catalog first. The worker's console script
imported `lakana_worker.__main__` and nothing before it, and failed.

The test lives here, not in a package's tests, because it must name modules of all four
packages. It only spawns subprocesses and reads files, so import-linter (which follows imports,
not strings) is not involved.
"""

import concurrent.futures
import contextlib
import json
import os
import signal
import subprocess  # nosec B404 -- fixed argv in tests
import sys
import tomllib
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parents[2] / "packages"

# Modules started with `python -m` by the Compose bundle (`lakana/cli/deploy/compose.yaml`,
# the `setup` service) and by the task harness (`LocalProcessRunner`).
_PYTHON_M_MODULES = ["lakana.cli.deploy.setup", "lakana.runner"]

# Run in one process: purge `lakana*` before each module so each import is a first import.
_EVERY_MODULE_SCRIPT = """
import importlib, json, sys, traceback
failures = {}
for name in json.loads(sys.argv[1]):
    print("importing", name, file=sys.stderr, flush=True)
    for loaded in [m for m in sys.modules if m.split(".")[0].startswith("lakana")]:
        del sys.modules[loaded]
    try:
        importlib.import_module(name)
    except BaseException:
        failures[name] = traceback.format_exc()[-1500:]
print(json.dumps(failures))
"""


_DROPPED_PREFIXES = ("COV_CORE_", "COVERAGE_", "PYTEST_", "XDIST_")


def _clean_environment() -> dict[str, str]:
    """Return the parent's environment without the variables of pytest, xdist and coverage.

    Those make a child interpreter start the parent's coverage measurement and plugin hooks,
    which has no place in a test that only imports modules.

    Returns
    -------
    dict
        The environment for the child; `PATH` and `HOME` stay.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith(_DROPPED_PREFIXES)}


def _run_isolated(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a child in its own process group and kill the whole group on a timeout.

    A child that hangs must not hang the test run: `subprocess.run` kills only the child, and a
    grandchild that keeps the pipes open would keep `communicate` waiting.

    Parameters
    ----------
    argv
        The command.
    timeout
        Seconds the child may take.

    Returns
    -------
    subprocess.CompletedProcess
        The finished child.

    Raises
    ------
    AssertionError
        If the child did not finish in time; the message ends with what it wrote to stderr, which
        names the module it was importing.
    """
    process = subprocess.Popen(  # nosec B603 -- fixed argv, the test interpreter
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_clean_environment(),
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            _, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stderr = "(its output could not be read: a grandchild holds the pipes)"
        raise AssertionError(
            f"the child did not finish in {timeout} s; the end of its progress was:\n"
            f"{stderr[-1000:]}"
        ) from None
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _entry_point_modules() -> list[str]:
    """Return the module of every `[project.scripts]` entry, plus the `python -m` modules.

    Returns
    -------
    list[str]
        Dotted module names, sorted, without duplicates.
    """
    modules = set(_PYTHON_M_MODULES)
    for pyproject in sorted(PACKAGES.glob("*/pyproject.toml")):
        scripts = tomllib.loads(pyproject.read_text()).get("project", {}).get("scripts", {})
        modules.update(target.split(":")[0] for target in scripts.values())
    return sorted(modules)


def _every_module() -> list[str]:
    """Return every importable module of the four packages except the Alembic migrations.

    Returns
    -------
    list[str]
        Dotted module names, sorted. `__init__` files stand for their package.
    """
    names = []
    for source in sorted(PACKAGES.glob("*/src")):
        for path in sorted(source.rglob("*.py")):
            parts = list(path.relative_to(source).with_suffix("").parts)
            if "migrations" in parts:
                continue
            if parts[-1] == "__init__":
                parts.pop()
            names.append(".".join(parts))
    return names


def test_the_entry_points_are_found() -> None:
    """The declared scripts and the `python -m` modules are all found."""
    assert {
        "lakana.cli.app",
        "lakana_server.__main__",
        "lakana_worker.__main__",
        "lakana.runner",
        "lakana.cli.deploy.setup",
    } <= set(_entry_point_modules())


@pytest.mark.parametrize("module", _entry_point_modules())
def test_an_entry_point_imports_alone_in_a_fresh_interpreter(module: str) -> None:
    """Import `module` as the first and only import of a new process.

    Parameters
    ----------
    module
        An entry point named in a `pyproject.toml` or started with `python -m`.
    """
    result = _run_isolated([sys.executable, "-c", f"import {module}"], timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]


def _chunks(modules: list[str], count: int) -> list[list[str]]:
    """Deal modules into `count` lists, so each child gets a similar mix of packages.

    Parameters
    ----------
    modules
        Dotted module names.
    count
        The most lists to return.

    Returns
    -------
    list of list of str
        Non-empty lists; every module is in exactly one.
    """
    return [chunk for chunk in (modules[i::count] for i in range(count)) if chunk]


def test_the_modules_are_dealt_to_the_children_without_loss() -> None:
    """Every module reaches exactly one child, whatever the number of children."""
    modules = [f"m{i}" for i in range(23)]
    for count in (1, 4, 50):
        dealt = [name for chunk in _chunks(modules, count) for name in chunk]
        assert sorted(dealt) == sorted(modules)


def test_every_module_imports_alone() -> None:
    """Import each module of the four packages as the first import of its process.

    The next cycle is caught whichever module it is in, not only in a listed entry point. The
    modules are shared among as many interpreters as the machine has CPUs, one after another in
    each, so every import is still the first of its interpreter.
    """
    modules = _every_module()
    assert len(modules) > 200, "the scan found too few modules; has the layout changed?"
    chunks = _chunks(modules, max(1, min(len(os.sched_getaffinity(0)), 8)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        results = list(
            pool.map(
                lambda chunk: _run_isolated(
                    [sys.executable, "-c", _EVERY_MODULE_SCRIPT, json.dumps(chunk)], timeout=300
                ),
                chunks,
            )
        )
    failures: dict[str, str] = {}
    for result in results:
        assert result.returncode == 0, result.stderr[-2000:]
        failures.update(json.loads(result.stdout.strip().splitlines()[-1]))
    assert not failures, "\n\n".join(f"{name}:\n{trace}" for name, trace in failures.items())


def test_a_hung_child_is_killed_with_its_whole_process_group_and_named() -> None:
    """The helper cannot hang: on a timeout it kills the group, and the message has the progress."""
    code = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
        "print(child.pid, flush=True); "
        "print('starting lakana.slow', file=sys.stderr, flush=True); time.sleep(600)"
    )
    with pytest.raises(AssertionError, match=r"(?s)did not finish.*lakana\.slow"):
        _run_isolated([sys.executable, "-c", code], timeout=3)


def test_the_child_environment_has_no_pytest_or_coverage_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage and xdist hooks of the parent run must not follow the child into its interpreter."""
    for name in (
        "COV_CORE_SOURCE",
        "COVERAGE_PROCESS_START",
        "PYTEST_XDIST_WORKER",
        "PYTEST_ADDOPTS",
    ):
        monkeypatch.setenv(name, "x")
    result = _run_isolated(
        [
            sys.executable,
            "-c",
            "import os; print(sorted(k for k in os.environ if k.startswith(('COV', 'PYTEST'))))",
        ],
        timeout=30,
    )
    assert result.stdout.strip() == "[]"
    assert _clean_environment()["PATH"] == os.environ["PATH"]
