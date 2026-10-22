"""Build a project's environment inside a build container, then check what was built.

Copied into every task image as `/opt/lakana-task/build.py`; the launcher runs it as the build
user with the project's code read-only at `/src/<build id>` (named by `LAKANA_BUILD_SRC`), its
environment volume at `/env` and the uv cache of its index set at `/uv-cache`. Standard library
only: the image has nothing else. The launcher marks the environment complete only when this
exits 0, in a file no build container can reach.

Exit status: uv's own when it fails (3, 4 and 5 become 1); `NO_SDK` when the environment has no
`lakana` harness to run an asset with; `NO_INTERPRETER` when uv made no `bin/python`;
`TOO_LARGE` when the environment is larger than `LAKANA_BUILD_MAX_BYTES` or has a directory the
check cannot read (a build could hide files that way); 1 when the limit is not a number.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 -- runs the image's own uv with a fixed argv, no shell
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

NO_SDK = 3
NO_INTERPRETER = 4
TOO_LARGE = 5
UV = "/usr/local/bin/uv"
UV_SYNC_FLAGS = (
    "sync",
    "--frozen",
    "--no-config",
    "--no-dev",
    "--no-default-groups",
    "--no-install-project",
    "--no-install-workspace",
)
"""The flags of every `uv sync` Lakana runs; the worker's venv cache uses this tuple too."""


def uv_argv(no_build: bool, python: str, project: Path) -> list[str]:
    """Write the `uv sync` command, the same flags as the worker's own venv cache.

    Parameters
    ----------
    no_build
        Whether to install wheels only: a credential is in the build's environment, and a
        package's own build code would see it.
    python
        The image's interpreter.
    project
        The directory holding `pyproject.toml` and `uv.lock`.

    Returns
    -------
    list of str
        The command.
    """
    return [
        UV,
        *UV_SYNC_FLAGS,
        *(["--no-build"] if no_build else []),
        "--python",
        python,
        "--project",
        str(project),
    ]


def _unreadable(error: OSError) -> None:
    """Refuse a directory that cannot be read, which would hide files from the size limit.

    Parameters
    ----------
    error
        What `os.walk` met.

    Raises
    ------
    OSError
        Always.
    """
    raise error


def tree_size(root: Path) -> int:
    """Add up the sizes of the files under a directory, without following links.

    Parameters
    ----------
    root
        The directory.

    Returns
    -------
    int
        Bytes.

    Raises
    ------
    OSError
        A directory could not be read: its files would not be counted.
    """
    total = 0
    for directory, _dirs, files in os.walk(root, onerror=_unreadable):
        for name in files:
            total += os.lstat(os.path.join(directory, name)).st_size
    return total


def main(
    environ: Mapping[str, str] = os.environ,
    environment: Path = Path("/env"),
    project: Path | None = None,
    run: Callable[[list[str]], int] = subprocess.call,  # nosec B603 -- fixed argv
) -> int:
    """Run uv, then check the environment it built.

    Parameters
    ----------
    environ
        The build's environment variables.
    environment
        Where uv builds (`UV_PROJECT_ENVIRONMENT`).
    project
        The project's code. Defaults to `LAKANA_BUILD_SRC`, else the working directory.
    run
        Runs a command and returns its status.

    Returns
    -------
    int
        The exit status described in the module docstring.
    """
    if project is None:
        project = Path(environ.get("LAKANA_BUILD_SRC") or Path.cwd())
    status = run(uv_argv(environ.get("LAKANA_BUILD_NO_BUILD") == "1", sys.executable, project))
    if status != 0:
        return 1 if status in (NO_SDK, NO_INTERPRETER, TOO_LARGE) else status
    if not (environment / "bin" / "python").exists():
        return NO_INTERPRETER
    if not any(environment.glob("lib/python*/site-packages/lakana/runner.py")):
        return NO_SDK
    limit = environ.get("LAKANA_BUILD_MAX_BYTES", "0")
    if not limit.isdigit():
        print(
            f"LAKANA_BUILD_MAX_BYTES is {limit!r}, not a number of bytes; set it to a whole "
            "number, or 0 for no limit",
            file=sys.stderr,
        )
        return 1
    try:
        too_large = int(limit) > 0 and tree_size(environment) > int(limit)
    except OSError as error:
        print(
            f"cannot measure the environment ({error}); a directory in it is unreadable, so "
            "the size limit cannot be checked",
            file=sys.stderr,
        )
        return TOO_LARGE
    return TOO_LARGE if too_large else 0


if __name__ == "__main__":
    sys.exit(main())
