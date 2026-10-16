"""Pidfiles for the local-mode child processes."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

_MAX_PORT = 65535


def write_pidfile(path: Path, pid: int, port: int | None = None) -> None:
    """Record a child's PID (and, for the server, its port).

    Writes to a fresh temp file (`O_CREAT | O_EXCL | O_NOFOLLOW`, random
    name) and then `os.replace`s it into place, the way `_profiles.save`
    writes the profile file — so a pre-existing symlink at `path` is
    replaced rather than written through.

    Parameters
    ----------
    path
        The pidfile.
    pid
        The process id.
    port
        The server's port, written as a second line (plan ruling 2).
    """
    lines = [str(pid)] if port is None else [str(pid), str(port)]
    text = "\n".join(lines) + "\n"
    temp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def read_pidfile(path: Path) -> tuple[int, int | None] | None:
    """Read a pidfile.

    Parameters
    ----------
    path
        The pidfile.

    Returns
    -------
    tuple or None
        `(pid, port)`, with `port` `None` when absent; `None` if the file is
        missing, unparsable, has more than two fields, the PID isn't a
        positive integer, or the port (when present) isn't in `1..65535`.
        A PID of `0` or a negative PID is rejected rather than merely
        "unusual": `os.kill(0, sig)` signals the whole process group and
        `os.kill(-1, sig)` signals every process this user may signal, so
        treating either as "the child we started" would make `pid_alive`
        answer a question about the wrong process entirely.
    """
    try:
        values = [int(line) for line in path.read_text().split()]
    except (FileNotFoundError, ValueError):
        return None
    if len(values) not in (1, 2):
        return None
    pid = values[0]
    if pid <= 0:
        return None
    if len(values) == 1:
        return pid, None
    port = values[1]
    if not (1 <= port <= _MAX_PORT):
        return None
    return pid, port


def pid_alive(pid: int) -> bool:
    """Report whether a process with this PID exists.

    Parameters
    ----------
    pid
        The process id.

    Returns
    -------
    bool
        True if it exists (it may belong to another program; callers treat
        this as a hint).
    """
    if os.name == "nt":  # pragma: no cover — Windows only; see plan ruling 4
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
        return bool(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove_pidfile(path: Path) -> None:
    """Delete a pidfile if it exists.

    Parameters
    ----------
    path
        The pidfile.
    """
    path.unlink(missing_ok=True)
