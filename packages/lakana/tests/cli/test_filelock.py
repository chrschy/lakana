"""Tests for the advisory file lock shared by profile updates and local mode."""

from __future__ import annotations

import errno
import os
import stat
import threading
from pathlib import Path

import pytest

from lakana.cli import _filelock
from lakana.cli._filelock import LockTimeout, locked, try_locked
from lakana.cli._state import CliError


def test_try_locked_excludes_a_second_holder_and_releases_on_exit(tmp_path: Path) -> None:
    """While one holder has the lock a second non-blocking attempt fails; afterwards it works."""
    path = tmp_path / "x.lock"
    with try_locked(path) as first:
        assert first is True
        with try_locked(path) as second:
            assert second is False
    with try_locked(path) as third:
        assert third is True


def test_try_locked_releases_after_an_exception(tmp_path: Path) -> None:
    """An exception inside the block still releases the lock.

    Raises
    ------
    RuntimeError
        Deliberately, inside the locked block.
    """
    path = tmp_path / "x.lock"
    with pytest.raises(RuntimeError), try_locked(path):
        raise RuntimeError("boom")
    with try_locked(path) as again:
        assert again is True


def test_the_lock_file_is_created_0600(tmp_path: Path) -> None:
    """The lock file is private to the user."""
    path = tmp_path / "x.lock"
    with try_locked(path):
        pass
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_symlinked_lock_path_is_refused(tmp_path: Path) -> None:
    """A symlink at the lock path is never followed."""
    target = tmp_path / "target"
    target.write_text("")
    path = tmp_path / "x.lock"
    path.symlink_to(target)
    with pytest.raises(CliError, match="symlink"), try_locked(path):
        pass
    with pytest.raises(CliError, match="symlink"), locked(path, timeout=1):
        pass


def test_an_unopenable_lock_path_is_a_clean_error(tmp_path: Path) -> None:
    """A lock path that is a directory is an exit-3 `CliError` naming the path."""
    path = tmp_path / "dir"
    path.mkdir()
    with pytest.raises(CliError) as excinfo, try_locked(path):
        pass
    assert excinfo.value.exit_code == 3
    assert str(path) in str(excinfo.value)


def test_locked_times_out_while_another_holder_keeps_the_lock(tmp_path: Path) -> None:
    """A blocking acquire gives up after its timeout."""
    path = tmp_path / "x.lock"
    with try_locked(path), pytest.raises(LockTimeout), locked(path, timeout=0.2):
        pass


def test_locked_waits_for_the_holder_to_release(tmp_path: Path) -> None:
    """A blocking acquire succeeds as soon as the holder lets go."""
    path = tmp_path / "x.lock"
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        """Hold the lock until told to release it."""
        with try_locked(path):
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(5)
    threading.Timer(0.2, release.set).start()
    with locked(path, timeout=5):
        assert os.path.exists(path)
    thread.join(5)


def test_the_lock_descriptor_is_not_inheritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Children never inherit the lock fd, so a dead supervisor can't leave it held."""
    real_open = _filelock._open
    fds: list[int] = []

    def recording_open(path: Path) -> int:
        """Open the lock file and remember its descriptor.

        Parameters
        ----------
        path
            The lock file.

        Returns
        -------
        int
            The descriptor.
        """
        fds.append(real_open(path))
        return fds[-1]

    monkeypatch.setattr(_filelock, "_open", recording_open)
    with try_locked(tmp_path / "x.lock"):
        assert os.get_inheritable(fds[0]) is False


@pytest.mark.parametrize("code", [errno.ENOLCK, errno.EOPNOTSUPP])
def test_an_unsupported_lock_is_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """A filesystem that can't lock (e.g. ENOLCK on NFS) is an exit-3 `CliError`."""

    def refuse(fd: int) -> bool:
        """Fail like a filesystem without lock support.

        Parameters
        ----------
        fd
            The descriptor.

        Raises
        ------
        OSError
            Always.
        """
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(_filelock, "_try_lock", refuse)
    path = tmp_path / "x.lock"
    for attempt in (try_locked(path), locked(path, timeout=1)):
        with pytest.raises(CliError) as excinfo, attempt:
            pass
        assert excinfo.value.exit_code == 3
        assert str(path) in str(excinfo.value)
