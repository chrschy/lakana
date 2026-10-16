"""An exclusive advisory lock on a dedicated lock file.

Used to serialize profile-file updates (`lakana.cli._profiles`) and to give a
local-mode instance sole ownership of its data directory
(`lakana.cli.local`). The lock lives on an open file description, so the OS
drops it when the holding process exits for any reason; a crashed holder never
blocks the next one. The lock file itself is never deleted (deleting it would
let two processes lock two different files at the same path).
"""

from __future__ import annotations

import errno
import os
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

_POLL_SECONDS = 0.05

if sys.platform == "win32":  # pragma: no cover — Windows only; see plan ruling 4
    import msvcrt

    def _try_lock(fd: int) -> bool:
        """Try to lock the first byte of `fd` without blocking.

        Parameters
        ----------
        fd
            An open file descriptor.

        Returns
        -------
        bool
            `True` if the lock was taken, `False` if another holder has it.
        """
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        """Try to take an exclusive `flock` on `fd` without blocking.

        Parameters
        ----------
        fd
            An open file descriptor.

        Returns
        -------
        bool
            `True` if the lock was taken, `False` if another holder has it.
        """
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


class LockTimeout(Exception):
    """A blocking acquire gave up because another process kept the lock."""


def _open(path: Path) -> int:
    """Open (creating, mode 0600) the lock file, refusing a symlink.

    Parameters
    ----------
    path
        The lock file.

    Returns
    -------
    int
        The open file descriptor.

    Raises
    ------
    CliError
        If `path` is a symlink (exit 2) or can't be opened (exit 3).
    """
    from lakana.cli._state import CliError

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CliError(f"{path} is a symlink; refusing to use it as a lock file") from exc
        raise CliError(f"Can't open lock file {path}: {exc}", 3) from exc


def _attempt(fd: int, path: Path) -> bool:
    """Try the lock once, reporting a filesystem that can't lock as a `CliError`.

    Parameters
    ----------
    fd
        The open lock file descriptor.
    path
        The lock file, for the error message.

    Returns
    -------
    bool
        `True` if the lock was taken, `False` if another holder has it.

    Raises
    ------
    CliError
        If locking fails for a reason other than contention, e.g. `ENOLCK`
        or `EOPNOTSUPP` on a filesystem without lock support (exit 3).
    """
    from lakana.cli._state import CliError

    try:
        return _try_lock(fd)
    except OSError as exc:
        raise CliError(
            f"Can't lock {path}: {exc}; does this filesystem support file locks?", 3
        ) from exc


@contextmanager
def try_locked(path: Path) -> Generator[bool, None, None]:
    """Try to take the exclusive lock without blocking.

    Parameters
    ----------
    path
        The lock file; created if missing.

    Yields
    ------
    bool
        `True` while this block holds the lock, `False` if another holder
        has it (nothing is held then).

    Raises
    ------
    CliError
        If the lock file is a symlink (exit 2), can't be opened, or the
        filesystem can't lock (exit 3).
    """
    fd = _open(path)
    try:
        yield _attempt(fd, path)
    finally:
        os.close(fd)


@contextmanager
def locked(path: Path, *, timeout: float) -> Generator[None, None, None]:
    """Take the exclusive lock, waiting up to `timeout` seconds.

    Parameters
    ----------
    path
        The lock file; created if missing.
    timeout
        Seconds to keep trying before giving up.

    Yields
    ------
    None
        While the lock is held.

    Raises
    ------
    LockTimeout
        If another holder kept the lock for the whole `timeout`.
    CliError
        If the lock file is a symlink (exit 2) or can't be opened (exit 3).
    """
    fd = _open(path)
    try:
        deadline = time.monotonic() + timeout
        while not _attempt(fd, path):
            if time.monotonic() >= deadline:
                raise LockTimeout(str(path))
            time.sleep(_POLL_SECONDS)
        yield
    finally:
        os.close(fd)
