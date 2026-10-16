"""Spill directories and the cleanup of those a stopped worker left behind.

Each query has a directory `lakana-spill-<pid>-<start>-<random>` that DuckDB may spill into,
up to `max_temp_directory_size` (10 GB), removed when the query ends. A worker that stops
(the memory watchdog, a kill) cannot remove it, and a restarted container keeps its ``/tmp``.
At start every worker removes the directories whose owner is gone.

Ownership: the name carries the creating process's id and its start time (field 22 of
``/proc/<pid>/stat``, in clock ticks since boot). A directory is removed when that process no
longer exists, or when the id now belongs to a process with another start time (the id was
reused). Where there is no ``/proc``, or for an older directory without an owner, the age
decides: a directory untouched for a day is removed.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PREFIX = "lakana-spill-"
MAX_AGE_SECONDS = 24 * 3600.0
"""How old an unowned directory (or any, without `/proc`) must be before it is removed."""
_OWNED = re.compile(r"lakana-spill-(\d+)-(\d+)-[A-Za-z0-9_]{8}")
_LEGACY = re.compile(r"lakana-spill-[A-Za-z0-9_]{8}")


def start_time(pid: int, proc: Path = Path("/proc")) -> int | None:
    """Read when a process started, in clock ticks since boot.

    Parameters
    ----------
    pid
        The process.
    proc
        The ``/proc`` directory.

    Returns
    -------
    int or None
        The start time, or `None` when the process does not exist or ``/proc`` has none.
    """
    try:
        return int((proc / str(pid) / "stat").read_text().rpartition(")")[2].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def spill_prefix(proc: Path = Path("/proc")) -> str:
    """Build the name prefix for this process's spill directories.

    Parameters
    ----------
    proc
        The ``/proc`` directory.

    Returns
    -------
    str
        ``lakana-spill-<pid>-<start>-``; the start is 0 where it cannot be read.
    """
    pid = os.getpid()
    return f"{PREFIX}{pid}-{start_time(pid, proc) or 0}-"


def _size(directory: Path) -> int:
    """Sum the sizes of the files below a directory, without following links.

    Parameters
    ----------
    directory
        The directory.

    Returns
    -------
    int
        Bytes.
    """
    total = 0
    for base, _, names in os.walk(directory, followlinks=False):
        for name in names:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                continue
    return total


def _abandoned(entry: Path, proc: Path, now: float) -> bool:
    """Decide whether a spill directory's owner is gone.

    Parameters
    ----------
    entry
        A directory directly under the temp directory, not a symlink.
    proc
        The ``/proc`` directory.
    now
        The current time.

    Returns
    -------
    bool
        Whether it should be removed.
    """
    owned = _OWNED.fullmatch(entry.name)
    if owned is not None and int(owned.group(2)) != 0 and proc.is_dir():
        return start_time(int(owned.group(1)), proc) != int(owned.group(2))
    if owned is None and _LEGACY.fullmatch(entry.name) is None:
        return False
    try:
        return now - entry.stat().st_mtime > MAX_AGE_SECONDS
    except OSError:
        return False


def sweep_spill_directories(
    temp: Path | None = None, proc: Path = Path("/proc"), now: float | None = None
) -> int:
    """Remove the spill directories that no live worker owns.

    Only directories directly under `temp` whose names match the exact pattern are
    considered; a symlink is never followed or removed.

    Parameters
    ----------
    temp
        The temp directory; `tempfile.gettempdir()` by default.
    proc
        The ``/proc`` directory.
    now
        The current time (a seam for tests).

    Returns
    -------
    int
        How many directories were removed. One log line gives the count and the bytes freed.
    """
    base = Path(tempfile.gettempdir()) if temp is None else temp
    moment = time.time() if now is None else now
    removed = freed = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(PREFIX) or entry.is_symlink() or not entry.is_dir():
            continue
        if not _abandoned(entry, proc, moment):
            continue
        size = _size(entry)
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed += 1
            freed += size
    if removed:
        logger.info(
            "Removed %d spill director%s left by a worker that stopped (%d MB freed).",
            removed,
            "y" if removed == 1 else "ies",
            freed // (1024 * 1024),
        )
    return removed
