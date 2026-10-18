"""Tests for local-mode pidfiles."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lakana.cli.local import _pidfile
from lakana.cli.local._pidfile import pid_alive, read_pidfile, remove_pidfile, write_pidfile


def test_round_trip_with_and_without_port(tmp_path: Path) -> None:
    """`server.pid` carries the port (plan ruling 2); `worker.pid` doesn't."""
    write_pidfile(tmp_path / "server.pid", 1234, 8765)
    write_pidfile(tmp_path / "worker.pid", 5678)

    assert read_pidfile(tmp_path / "server.pid") == (1234, 8765)
    assert read_pidfile(tmp_path / "worker.pid") == (5678, None)


def test_missing_or_garbage_pidfiles_read_as_none(tmp_path: Path) -> None:
    """A missing or unparsable pidfile is treated as absent."""
    (tmp_path / "bad.pid").write_text("not a pid\n")

    assert read_pidfile(tmp_path / "none.pid") is None
    assert read_pidfile(tmp_path / "bad.pid") is None


def test_an_empty_pidfile_reads_as_none(tmp_path: Path) -> None:
    """A pidfile with no content at all (e.g. truncated mid-write) is absent."""
    path = tmp_path / "empty.pid"
    path.write_text("   \n")

    assert read_pidfile(path) is None


@pytest.mark.parametrize(
    "content",
    ["-1", "0", "1 2 3", "5 0", "5 70000"],
)
def test_out_of_range_pidfiles_read_as_none(tmp_path: Path, content: str) -> None:
    """A negative or zero PID, a bad port, or extra fields all read as absent.

    `kill(-1, sig)` signals every process this user can signal and `kill(0,
    sig)` signals the whole process group, so a PID that isn't a positive
    integer must never be trusted as "the child we started".
    """
    path = tmp_path / "server.pid"
    path.write_text(content + "\n")

    assert read_pidfile(path) is None


def test_write_pidfile_replaces_a_symlink_without_following_it(tmp_path: Path) -> None:
    """A pre-existing symlink at the pidfile path is replaced, not written through."""
    target = tmp_path / "real_target"
    target.write_text("untouched")
    link = tmp_path / "server.pid"
    link.symlink_to(target)

    write_pidfile(link, 4321)

    assert target.read_text() == "untouched"
    assert not link.is_symlink()
    assert read_pidfile(link) == (4321, None)


def test_write_pidfile_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    """The atomic write doesn't litter the directory with its temp file."""
    write_pidfile(tmp_path / "server.pid", 1, 2)

    assert {p.name for p in tmp_path.iterdir()} == {"server.pid"}


def test_write_pidfile_cleans_up_its_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the final `os.replace` fails, the temp file doesn't linger behind."""

    def boom(src: object, dst: object) -> None:
        """Simulate `os.replace` failing after the temp file was written.

        Parameters
        ----------
        src
            The temp file.
        dst
            The destination pidfile.

        Raises
        ------
        OSError
            Always.
        """
        raise OSError("simulated replace failure")

    monkeypatch.setattr(_pidfile.os, "replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        write_pidfile(tmp_path / "server.pid", 1)

    assert list(tmp_path.iterdir()) == []


def test_pid_alive() -> None:
    """Confirm this process is alive; a PID just reaped by `wait()` is not.

    A spawned-then-waited child gives a deterministically dead PID without
    assuming anything about the host's `pid_max` (unlike a hardcoded huge
    PID, which only works if it exceeds the kernel's configured maximum).
    """
    assert pid_alive(os.getpid()) is True

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()

    assert pid_alive(child.pid) is False


def test_pid_alive_treats_permission_denied_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PID that exists but isn't ours to signal (`EPERM`) still counts as alive."""

    def deny(pid: int, sig: int) -> None:
        """Simulate signalling a process this user doesn't own.

        Parameters
        ----------
        pid
            The target process id.
        sig
            The signal number.

        Raises
        ------
        PermissionError
            Always.
        """
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(_pidfile.os, "kill", deny)

    assert pid_alive(12345) is True


def test_remove_is_idempotent(tmp_path: Path) -> None:
    """Removing a missing pidfile is fine."""
    path = tmp_path / "server.pid"
    write_pidfile(path, 1)

    remove_pidfile(path)
    remove_pidfile(path)

    assert not path.exists()


def test_the_lock_owner_round_trips_and_clears(tmp_path: Path) -> None:
    """The supervisor's PID is kept in the lock file, so `server stop` can find it."""
    lock = tmp_path / "lock"
    lock.touch()
    assert _pidfile.read_lock_owner(lock) is None
    _pidfile.write_lock_owner(lock, 4321)
    assert _pidfile.read_lock_owner(lock) == 4321
    _pidfile.clear_lock_owner(lock)
    assert _pidfile.read_lock_owner(lock) is None


def test_garbage_in_the_lock_file_reads_as_no_owner(tmp_path: Path) -> None:
    """A lock file with text, a non-positive number or no file at all has no owner."""
    lock = tmp_path / "lock"
    assert _pidfile.read_lock_owner(lock) is None
    for text in ("hello", "-5", "0", "1 2"):
        lock.write_text(text)
        assert _pidfile.read_lock_owner(lock) is None


def test_clearing_a_lock_file_that_is_gone_is_not_an_error(tmp_path: Path) -> None:
    """The holder may leave after the file was removed; clearing it then does nothing."""
    lock = tmp_path / "missing-lock"

    _pidfile.clear_lock_owner(lock)

    assert not lock.exists()
