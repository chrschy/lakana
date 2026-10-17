"""`lakana server reset`: what a local instance owns, and the checks before any of it is deleted.

The reset deletes a fixed list of names directly under the instance's data directory, never
anything found by walking or matching. Every check that can refuse runs before the first
deletion.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from lakana.cli._state import CliError
from lakana.cli.local._datadir import MARKER_NAME, MARKER_TEXT, resolve_data_dir

_OWNED = (
    "lakana.db",
    "lakana.db-wal",
    "lakana.db-shm",
    "lakana.db-journal",
    "lake",
    "master.key",
    "server.pid",
    "worker.pid",
)
"""The names, directly under the data directory, that a reset deletes. Nothing else."""

_START_HINT = (
    "an instance that this version of lakana never started has no marker: run "
    "'lakana server start --local' once (then stop it with Ctrl-C), which writes it, "
    "and try again"
)


def _has_marker(root: Path) -> bool:
    """Report whether `root` holds the genuine marker: a regular file with the exact content.

    Parameters
    ----------
    root
        The resolved data directory.

    Returns
    -------
    bool
        `False` for a missing marker, a symlink, another kind of file or other content.
    """
    path = root / MARKER_NAME
    try:
        if path.is_symlink() or not path.is_file():
            return False
        return path.read_text() == MARKER_TEXT
    except (OSError, UnicodeDecodeError):
        return False


def _count_tables(db_path: Path) -> int | None:
    """Count the catalog's tables, read-only.

    Parameters
    ----------
    db_path
        The metadata database.

    Returns
    -------
    int or None
        The number of rows of the catalog's `tables` table; `None` if the database can't be
        read (the reset goes on: it is about to delete it).
    """
    try:
        with sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True) as db:
            return int(db.execute("SELECT COUNT(*) FROM tables").fetchone()[0])
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ResetPlan:
    """What a reset will delete.

    Parameters
    ----------
    root
        The data directory with symlinks resolved.
    targets
        The existing paths, each directly under `root` and none a symlink.
    tables
        The number of tables in the catalog, or `None` if it could not be read.
    """

    root: Path
    targets: tuple[Path, ...]
    tables: int | None

    def describe(self) -> list[str]:
        """Say what will be deleted.

        Returns
        -------
        list of str
            One line for the directory, one per path and one for the tables.
        """
        if self.tables is None:
            count = "an unknown number of tables"
        else:
            count = f"{self.tables} table{'s' if self.tables != 1 else ''}"
        return [
            f"This deletes the data of the local Lakana instance in {self.root}:",
            *(f"  {path}" for path in self.targets),
            f"  {count} in the catalog, with all their data, and every secret, token and run.",
            "Your project files are not touched.",
        ]


def refuse_unsafe_root(path: Path, home: Path) -> Path | None:
    """Check that `path` is a local instance's data directory and nothing else.

    Parameters
    ----------
    path
        The data directory as given.
    home
        The user's home directory.

    Returns
    -------
    Path or None
        The directory with symlinks resolved; `None` if it does not exist, which means there
        is nothing to reset.

    Raises
    ------
    CliError
        If the path is a symlink or not a directory; is a filesystem root, the home directory
        or a directory that holds it; or lacks the marker (exit 2).
    """
    given = resolve_data_dir(path)
    if not os.path.lexists(given):
        return None
    if given.is_symlink():
        raise CliError(
            f"{given} is a symlink; refusing to reset through it",
            hint="pass the real directory with --data-dir",
        )
    root = given.resolve()
    if not root.is_dir():
        raise CliError(f"{given} is not a directory")
    if root.parent == root:
        raise CliError(f"{root} is a filesystem root; refusing to reset it")
    home_real = home.resolve()
    if root == home_real or root in home_real.parents:
        raise CliError(
            f"{root} is your home directory or holds it; refusing to reset it",
            hint="the local instance's data directory is a folder of its own: "
            "see 'lakana server status'",
        )
    if not _has_marker(root):
        raise CliError(
            f"{root} does not look like a Lakana local instance (no {MARKER_NAME} file); "
            "nothing was deleted",
            hint=_START_HINT,
        )
    return root


def plan_reset(root: Path) -> ResetPlan:
    """List what a reset deletes in a checked data directory.

    Parameters
    ----------
    root
        The resolved directory `refuse_unsafe_root` returned.

    Returns
    -------
    ResetPlan
        The existing owned paths and the table count.

    Raises
    ------
    CliError
        If any owned path is a symlink or does not resolve to a direct child of `root`
        (exit 2); nothing has been deleted.
    """
    targets: list[Path] = []
    for name in _OWNED:
        path = root / name
        if not os.path.lexists(path):
            continue
        if path.is_symlink() or path.resolve().parent != root:
            raise CliError(
                f"{path} is a symlink; refusing to delete through it",
                hint=f"remove the link yourself if you want it gone; nothing in {root} was deleted",
            )
        targets.append(path)
    return ResetPlan(root, tuple(targets), _count_tables(root / "lakana.db"))


def delete_targets(plan: ResetPlan) -> None:
    """Delete the planned paths, checking each again just before it goes.

    A directory is removed with `shutil.rmtree`, which unlinks a symlink inside it and never
    follows it.

    Parameters
    ----------
    plan
        The plan.

    Raises
    ------
    CliError
        If a path became a symlink since the plan was made (exit 2), or a deletion fails
        (exit 3, naming the path and what is left).
    """
    for path in plan.targets:
        if path.is_symlink() or (os.path.lexists(path) and path.resolve().parent != plan.root):
            raise CliError(f"{path} became a symlink; refusing to delete through it")
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            raise CliError(
                f"Can't delete {path}: {exc}",
                3,
                hint="fix the cause and run the reset again; it deletes what is left",
            ) from exc
