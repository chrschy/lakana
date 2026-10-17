"""`lakana server reset`: what a local instance owns, and the checks before any of it is deleted.

The reset deletes a fixed list of names directly under the instance's data directory, never
anything found by walking or matching. Every check that can refuse runs before the first
deletion.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from lakana.cli._state import CliError
from lakana.cli.local._datadir import MARKER_NAME, MARKER_TEXT, resolve_data_dir

_OWNED = (
    "lake",
    "master.key",
    "server.pid",
    "worker.pid",
    "lakana.db-wal",
    "lakana.db-shm",
    "lakana.db-journal",
    "lakana.db",
)
"""The names, directly under the data directory, that a reset deletes. Nothing else.

The database is last: a reset that fails part-way leaves a directory that still has its
database, so the next start is not a first run on top of old data, and a second reset finishes.
"""

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
            f"  {count} in the catalog, and every secret, token and run.",
            "The tables' files under lake/ go with them. Files of an environment whose storage "
            "is outside lake/ stay where they are.",
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
        raise CliError(
            f"{given} is not a directory",
            hint="pass the local instance's data directory with --data-dir "
            "(see 'lakana server status')",
        )
    if root.parent == root:
        raise CliError(f"{root} is a filesystem root; refusing to reset it")
    if _is_or_holds_home(root, home):
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


def _is_or_holds_home(root: Path, home: Path) -> bool:
    """Report whether `root` is a home directory or one of the directories that hold it.

    Compared by device and inode, so a second path to the same directory (a bind mount, a
    link in a parent) counts. Both `$HOME` and the home in the password database are checked.

    Parameters
    ----------
    root
        The resolved data directory.
    home
        The user's home directory as `Path.home()` reports it.

    Returns
    -------
    bool
        `True` if `root` is any of them.
    """
    homes = [home]
    try:
        import pwd  # not on Windows
    except ImportError:  # pragma: no cover -- Windows only
        pass
    else:
        with contextlib.suppress(KeyError, OSError):
            homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    for candidate in homes:
        try:
            chain = [candidate.resolve(), *candidate.resolve().parents]
        except OSError:
            continue
        for directory in chain:
            try:
                if os.path.samefile(root, directory):
                    return True
            except OSError:
                continue
    return False


def _device_of(path: Path) -> int:
    """Return the device a path lives on, without following a symlink.

    Parameters
    ----------
    path
        The path.

    Returns
    -------
    int
        `st_dev` of the path itself.
    """
    return os.lstat(path).st_dev


_MOUNTINFO = Path("/proc/self/mountinfo")
"""The kernel's mount table, where there is one."""


def _mount_points() -> list[Path] | None:
    """List this process's mount points.

    Returns
    -------
    list of Path or None
        The mount points from `/proc/self/mountinfo` (field 5, octal escapes undone);
        `None` where the platform has no such file and only the device check applies.

    Raises
    ------
    CliError
        On Linux, if the table can't be read: the reset can't tell what is mounted, so it
        refuses (exit 2).
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        text = _MOUNTINFO.read_text()
    except OSError as exc:
        raise CliError(
            f"Can't read {_MOUNTINFO} ({exc}), so the reset can't tell whether a mount lies "
            "inside the instance's data. Nothing was deleted",
            hint="make /proc readable for this process, or delete the instance's files by hand",
        ) from exc
    points: list[Path] = []
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) > 4:
            points.append(
                Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
            )
    return points


def _find_mount(root: Path, targets: list[Path]) -> Path | None:
    """Find a mount point that is a target or lies inside one, on any device.

    Two checks: the kernel's mount table (catches a bind mount, which shares the device of
    its surroundings), and the device of every path below the targets (where there is no mount
    table). Symlinks are never followed.

    Parameters
    ----------
    root
        The resolved data directory.
    targets
        The owned paths that exist.

    Returns
    -------
    Path or None
        The first mount point found, or `None`.

    Raises
    ------
    CliError
        If a directory can't be scanned (exit 3) or, on Linux, the mount table can't be read.
    """
    points = _mount_points()
    if points is not None:
        for point in points:
            for target in targets:
                if point == target or target in point.parents:
                    return point
    device = _device_of(root)
    pending = list(targets)
    while pending:
        path = pending.pop()
        try:
            if _device_of(path) != device:
                return path
            if path.is_dir() and not path.is_symlink():
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
        except PermissionError as exc:
            raise CliError(
                f"Can't read {path}: {exc}. Nothing was deleted",
                3,
                hint=f"give yourself access (chmod u+rwx {path}), then run the reset again",
            ) from exc
    return None


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
                f"{path} is a symlink or is not inside {root}; refusing to delete through it",
                hint=f"remove the link yourself if you want it gone; nothing in {root} was deleted",
            )
        targets.append(path)
    mount = _find_mount(root, targets)
    if mount is not None:
        raise CliError(
            f"{mount} is a mount point inside the instance's data in {root}; "
            "refusing to delete across it. Nothing was deleted",
            hint=f"unmount {mount} or remove it yourself, then run the reset again",
        )
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
        If a path was replaced or moved since the plan was made (exit 2), or a deletion fails
        (exit 3, naming the path and what is left).
    """
    for position, path in enumerate(plan.targets):
        if path.is_symlink() or (os.path.lexists(path) and path.resolve().parent != plan.root):
            raise CliError(
                f"{path} is not inside {plan.root} any more (it was replaced by a link or "
                "moved); refusing to delete through it",
                hint="check what changed in the data directory, then run the reset again",
            )
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            chmod = (
                f" Give yourself access first: chmod -R u+rwx {path}."
                if isinstance(exc, PermissionError)
                else ""
            )
            left = ", ".join(str(p) for p in plan.targets[position:] if os.path.lexists(p))
            raise CliError(
                f"Can't delete {path}: {exc}. Still there: {left}",
                3,
                hint=f"fix the cause and run the reset again; it deletes what is left.{chmod} The "
                "database goes last, so the instance is not reset until that run succeeds",
            ) from exc
