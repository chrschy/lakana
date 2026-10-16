"""Whether this worker can run Python assets, and where it keeps its caches."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lakana_core.config import RunConfig

CANNOT_RUN = "this instance cannot run Python assets: {reason}"
"""The deploy issue and the task failure when `PythonSupport.reason()` is not `None`."""


@dataclass(frozen=True)
class PythonSupport:
    """What a worker needs to run a Python asset.

    Parameters
    ----------
    server_url
        `LAKANA_RUN__SERVER_URL`, or `None`.
    uv
        Absolute path of the `uv` executable, or `None` when it is not on `PATH`.
    """

    server_url: str | None = None
    uv: str | None = None

    def reason(self) -> str | None:
        """Say why Python assets cannot run here.

        Returns
        -------
        str or None
            The first missing requirement, or `None` when everything is there.
        """
        if self.server_url is None:
            return "LAKANA_RUN__SERVER_URL is not set"
        if self.uv is None:
            return "uv is not on the worker's PATH"
        return None

    def fix(self) -> str:
        """Say what an operator does about `reason()`.

        Returns
        -------
        str
            The next step for the first missing requirement; empty when nothing is missing.
        """
        if self.server_url is None:
            return (
                "set LAKANA_RUN__SERVER_URL to the address the worker reaches the server at "
                "(Compose bundle: LAKANA_RUN__SERVER_URL=http://server:8000; local mode sets "
                "it itself)"
            )
        if self.uv is None:
            return (
                "install uv where the worker runs and put it on the worker's PATH "
                "(the Lakana image ships it)"
            )
        return ""


def detect(run: RunConfig) -> PythonSupport:
    """Look at the settings and the `PATH` once, at worker start.

    Parameters
    ----------
    run
        The run settings.

    Returns
    -------
    PythonSupport
        The server URL and where `uv` is (an absolute path: the child runs elsewhere).
    """
    found = shutil.which("uv")
    return PythonSupport(
        server_url=run.server_url, uv=None if found is None else os.path.abspath(found)
    )


def worker_cache_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the worker's cache directory (venvs, per-attempt directories, uv's cache).

    Parameters
    ----------
    environ
        The environment to read; `os.environ` when `None`.

    Returns
    -------
    Path
        ``$XDG_CACHE_HOME/lakana-worker`` when that variable is an absolute
        path, else ``~/.cache/lakana-worker``.
    """
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CACHE_HOME", "")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / "lakana-worker"
    home = env.get("HOME")
    base = Path(home) if home else Path.home()
    return base / ".cache" / "lakana-worker"
