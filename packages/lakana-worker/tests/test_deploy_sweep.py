"""The sweep of deploy checkouts a killed worker left behind."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from lakana_core.projects.limits import DeployLimits
from lakana_worker import deploy_sweep
from lakana_worker.deploy_sweep import MIN_AGE_SECONDS, DeploySweeper, hold_checkout

_NOW = 10_000_000.0
_OLD = MIN_AGE_SECONDS + 1


@pytest.fixture
def temp(tmp_path: Path) -> Path:
    """Provide a temp directory like ``/var/tmp/lakana``.

    Parameters
    ----------
    tmp_path
        Pytest's temporary directory.

    Returns
    -------
    Path
        The directory.
    """
    path = tmp_path / "temp"
    path.mkdir()
    return path


def _sweeper(temp: Path, *, interval: float = 3600.0) -> DeploySweeper:
    """Build a sweeper with a frozen clock.

    Parameters
    ----------
    temp
        The temp directory.
    interval
        Seconds between sweeps.

    Returns
    -------
    DeploySweeper
        The sweeper.
    """
    return DeploySweeper(temp, interval=interval, clock=lambda: _NOW)


def _directory(temp: Path, name: str, age: float) -> Path:
    """Create a directory with a file in it and a given age.

    Parameters
    ----------
    temp
        The temp directory.
    name
        The directory name.
    age
        Seconds before `_NOW` its mtime is set to.

    Returns
    -------
    Path
        The directory.
    """
    path = temp / name
    path.mkdir(mode=0o700)
    (path / "repo.tar").write_text("x")
    os.utime(path, (_NOW - age, _NOW - age))
    return path


def test_the_age_guard_exceeds_the_deploy_job_time_limit() -> None:
    assert MIN_AGE_SECONDS == 24 * 3600
    assert DeployLimits().job_seconds < MIN_AGE_SECONDS


def test_a_longer_job_limit_raises_the_age_guard(temp: Path) -> None:
    old = _directory(temp, "lakana-deploy-abcd1234", MIN_AGE_SECONDS + 10)
    sweeper = DeploySweeper(temp, max_age=MIN_AGE_SECONDS * 2, clock=lambda: _NOW)
    assert sweeper.sweep() == 0
    assert old.exists()


def test_an_old_unlocked_checkout_is_removed(temp: Path) -> None:
    old = _directory(temp, "lakana-deploy-abcd1234", _OLD)
    assert _sweeper(temp).sweep() == 1
    assert not old.exists()


def test_a_locked_checkout_stays_however_old(temp: Path) -> None:
    held = _directory(temp, "lakana-deploy-abcd1234", _OLD * 100)
    with hold_checkout(held):
        assert _sweeper(temp).sweep() == 0
        assert held.exists()
    assert _sweeper(temp).sweep() == 1
    assert not held.exists()


def test_a_young_unlocked_checkout_stays(temp: Path) -> None:
    young = _directory(temp, "lakana-deploy-abcd1234", MIN_AGE_SECONDS - 1)
    assert _sweeper(temp).sweep() == 0
    assert young.exists()


def test_a_symlink_named_like_a_checkout_is_not_followed_or_removed(
    temp: Path, tmp_path: Path
) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep").write_text("x")
    os.utime(target, (_NOW - _OLD, _NOW - _OLD))
    link = temp / "lakana-deploy-abcd1234"
    link.symlink_to(target, target_is_directory=True)
    assert _sweeper(temp).sweep() == 0
    assert link.is_symlink()
    assert (target / "keep").exists()


def test_a_file_named_like_a_checkout_stays(temp: Path) -> None:
    path = temp / "lakana-deploy-abcd1234"
    path.write_text("x")
    os.utime(path, (_NOW - _OLD, _NOW - _OLD))
    assert _sweeper(temp).sweep() == 0
    assert path.exists()


@pytest.mark.parametrize(
    "name",
    [
        "lakana-deploy-abcd123",
        "lakana-deploy-abcd12345",
        "lakana-deploy-ABCD1234",
        "lakana-deploy-abcd1234.bak",
        "x-lakana-deploy-abcd1234",
        "lakana-spill-abcd1234",
        "lakana-spill-1234-5678-abcd1234",
        "attempt-abcd1234",
        "other",
    ],
)
def test_a_directory_of_another_name_stays(temp: Path, name: str) -> None:
    other = _directory(temp, name, _OLD * 10)
    assert _sweeper(temp).sweep() == 0
    assert other.exists()


def test_a_directory_replaced_between_check_and_removal_stays(
    temp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _directory(temp, "lakana-deploy-abcd1234", _OLD)
    real = os.open
    swapped: list[bool] = []

    def open_and_swap(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        descriptor = real(path, flags, *args)
        if Path(path) == old and not swapped:
            swapped.append(True)
            old.rename(temp / "moved")
            old.mkdir(mode=0o700)
            (old / "live").write_text("x")
            os.utime(old, (_NOW - _OLD, _NOW - _OLD))
        return descriptor

    monkeypatch.setattr(os, "open", open_and_swap)
    assert _sweeper(temp).sweep() == 0
    assert (old / "live").exists()


def test_a_removal_that_fails_is_skipped_without_raising(
    temp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _directory(temp, "lakana-deploy-abcd1234", _OLD)
    second = _directory(temp, "lakana-deploy-efgh5678", _OLD)
    calls: list[Path] = []

    def remove(path: Path) -> bool:
        calls.append(path)
        if len(calls) == 1:
            raise OSError(5, "Input/output error")
        return False

    monkeypatch.setattr(deploy_sweep, "remove_private_tree", remove)
    assert _sweeper(temp).sweep() == 0
    assert len(calls) == 2
    assert second.exists()


def test_a_lock_that_cannot_be_taken_is_logged_and_the_checkout_kept(
    temp: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    kept = _directory(temp, "lakana-deploy-abcd1234", _OLD)

    def refuse(descriptor: int, path: Path) -> bool:
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(deploy_sweep, "lock_exclusive_if_same", refuse)
    sweeper = _sweeper(temp)
    with caplog.at_level(logging.WARNING):
        assert sweeper.sweep() == 0
        assert sweeper.sweep() == 0
    assert kept.exists()
    assert "Operation not supported" in caplog.text
    assert len(caplog.records) == 1  # once per directory and process, as AttemptSweeper does
    assert "kept" in caplog.text and "24 hours" not in caplog.text


def test_an_unlistable_temp_directory_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert _sweeper(tmp_path / "missing").sweep() == 0
    assert "could not be listed" in caplog.text


def test_a_deploy_that_cannot_lock_goes_on(
    temp: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    checkout = _directory(temp, "lakana-deploy-abcd1234", 0)

    def refuse(descriptor: int, operation: int) -> None:
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(deploy_sweep.fcntl, "flock", refuse)
    with caplog.at_level(logging.WARNING), hold_checkout(checkout):
        pass
    assert "could not be locked" in caplog.text


def test_removal_logs_the_count_and_maybe_sweep_is_paced(
    temp: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = [0.0]
    sweeper = DeploySweeper(temp, interval=100.0, clock=lambda: _NOW, monotonic=lambda: clock[0])
    first = _directory(temp, "lakana-deploy-abcd1234", _OLD)
    with caplog.at_level(logging.INFO):
        sweeper.maybe_sweep()
    assert not first.exists()
    assert "Removed 1 deploy checkout" in caplog.text
    second = _directory(temp, "lakana-deploy-efgh5678", _OLD)
    clock[0] = 99.0
    sweeper.maybe_sweep()
    assert second.exists()
    clock[0] = 100.0
    sweeper.maybe_sweep()
    assert not second.exists()


def test_an_interval_of_zero_disables_maybe_sweep(temp: Path) -> None:
    old = _directory(temp, "lakana-deploy-abcd1234", _OLD)
    _sweeper(temp, interval=0).maybe_sweep()
    assert old.exists()


def test_a_directory_of_another_user_stays(temp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = _directory(temp, "lakana-deploy-abcd1234", _OLD)
    other_uid = os.geteuid() + 1
    monkeypatch.setattr(deploy_sweep.os, "geteuid", lambda: other_uid)
    assert _sweeper(temp).sweep() == 0
    assert other.exists()


def test_a_directory_swapped_for_a_young_one_before_it_is_opened_stays(
    temp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rules are decided on what was opened, not on the path looked at before."""
    old = _directory(temp, "lakana-deploy-abcd1234", _OLD)
    real = os.open
    swapped: list[bool] = []

    def swap_then_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        if Path(path) == old and not swapped:
            swapped.append(True)
            old.rename(temp / "moved")
            old.mkdir(mode=0o700)
            (old / "live").write_text("x")
            os.utime(old, (_NOW - 10, _NOW - 10))
        return real(path, flags, *args)

    monkeypatch.setattr(os, "open", swap_then_open)
    assert _sweeper(temp).sweep() == 0
    assert (old / "live").exists()


def test_a_symlink_swapped_in_before_it_is_opened_is_not_followed(
    temp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _directory(temp, "lakana-deploy-abcd1234", _OLD)
    target = tmp_path / "precious"
    target.mkdir()
    os.utime(target, (_NOW - _OLD, _NOW - _OLD))
    real = os.open

    def swap_then_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        if Path(path) == old and old.is_dir() and not old.is_symlink():
            old.rename(temp / "moved")
            old.symlink_to(target, target_is_directory=True)
        return real(path, flags, *args)

    monkeypatch.setattr(os, "open", swap_then_open)
    assert _sweeper(temp).sweep() == 0
    assert target.exists()


def test_a_checkout_that_cannot_be_opened_is_held_by_nobody_and_logged(
    temp: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING), hold_checkout(temp / "missing"):
        pass
    assert "could not be locked" in caplog.text


def test_the_once_per_directory_warnings_are_bounded(
    temp: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first = _directory(temp, "lakana-deploy-abcd1234", _OLD)
    second = _directory(temp, "lakana-deploy-efgh5678", _OLD)

    def refuse(descriptor: int, path: Path) -> bool:
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(deploy_sweep, "lock_exclusive_if_same", refuse)
    monkeypatch.setattr(deploy_sweep, "MAX_WARNED", 1)
    sweeper = _sweeper(temp)
    with caplog.at_level(logging.WARNING):
        sweeper.sweep()
        sweeper.sweep()
    assert len(caplog.records) == 1
    assert len(sweeper._warned) == 1
    assert first.exists() and second.exists()
