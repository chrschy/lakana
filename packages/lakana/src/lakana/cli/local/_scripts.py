"""Locate the `lakana-server` / `lakana-worker` console scripts without importing them."""

from __future__ import annotations

import os
import sysconfig
from importlib.metadata import Distribution, EntryPoint, entry_points
from pathlib import Path

from lakana.cli._state import CliError

_INSTALL_HINT = 'Local mode needs the server packages: pip install "lakana[server]"'


def _scripts_dir() -> Path:
    """Return this environment's console-script directory.

    Returns
    -------
    Path
        `sysconfig.get_path("scripts")`.
    """
    return Path(sysconfig.get_path("scripts"))


def _script_suffix() -> str:
    """Return the executable suffix console scripts get on this platform.

    Returns
    -------
    str
        `".exe"` on Windows, otherwise `""`.
    """
    if os.name == "nt":  # pragma: no cover — Windows only; see plan ruling 4
        return ".exe"
    return ""


def _script_from_distribution(dist: Distribution | None, name: str) -> Path | None:
    """Resolve a console script's real path from its distribution's file list.

    This is what makes lookup correct under e.g. `pip install --user` (whose
    scripts directory differs from `sysconfig`'s) and stops it from picking
    up another installation's same-named script from that directory.

    Parameters
    ----------
    dist
        The entry point's distribution, if any.
    name
        The script's console-script name.

    Returns
    -------
    Path or None
        The script file the distribution's `RECORD` names, resolved with
        `Distribution.locate_file`; `None` if the distribution (or its file
        list) isn't available, so the caller falls back to `_scripts_dir`.
    """
    if dist is None:
        return None
    files = dist.files
    if not files:
        return None
    target_name = f"{name}{_script_suffix()}"
    scripts_folder = "Scripts" if os.name == "nt" else "bin"
    for entry in files:
        if entry.name == target_name and scripts_folder in entry.parts:
            return Path(str(dist.locate_file(entry)))
    return None


def find_script(name: str) -> Path:
    """Find an installed console script by name, using package metadata only.

    Parameters
    ----------
    name
        `"lakana-server"` or `"lakana-worker"`.

    Returns
    -------
    Path
        The executable script in this environment.

    Raises
    ------
    CliError
        If the entry point isn't registered or its script isn't installed
        (exit 3, with the `lakana[server]` install hint).
    """
    matches: list[EntryPoint] = list(entry_points(group="console_scripts", name=name))
    if not matches:
        raise CliError(_INSTALL_HINT, 3, kind="MissingServerExtra")
    path = _script_from_distribution(matches[0].dist, name)
    if path is None:
        path = _scripts_dir() / f"{name}{_script_suffix()}"
    if not path.exists():
        raise CliError(_INSTALL_HINT, 3, kind="MissingServerExtra")
    return path
