"""Tests for the cleanup of spill directories a stopped worker left behind."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import pytest

from lakana_worker.spill import (
    MAX_AGE_SECONDS,
    spill_prefix,
    start_time,
    sweep_spill_directories,
)

NOW = 1_000_000.0


def _proc(tmp_path: Path, live: dict[int, int]) -> Path:
    """Build a fake ``/proc`` with live processes and their start times.

    Parameters
    ----------
    tmp_path
        Scratch directory.
    live
        Process id to start time.

    Returns
    -------
    Path
        The fake directory.
    """
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, start in live.items():
        (proc / str(pid)).mkdir()
        fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 5
        (proc / str(pid) / "stat").write_text(f"{pid} (a b) {' '.join(fields)}\n")
    return proc


def _spill(temp: Path, name: str, size: int = 1024, age: float = 0.0) -> Path:
    """Create a spill directory with one file in it.

    Parameters
    ----------
    temp
        The temp directory.
    name
        The directory name.
    size
        Bytes of its file.
    age
        Seconds since it was last modified.

    Returns
    -------
    Path
        The directory.
    """
    directory = temp / name
    directory.mkdir()
    (directory / "spill.tmp").write_bytes(b"x" * size)
    stamp = NOW - age
    os.utime(directory, (stamp, stamp))
    return directory


def test_the_start_time_is_read_from_proc_stat(tmp_path: Path) -> None:
    """Field 22, even when the command name holds spaces and parentheses."""
    proc = _proc(tmp_path, {7: 424242})
    assert start_time(7, proc) == 424242
    assert start_time(8, proc) is None
    (proc / "9").mkdir()
    (proc / "9" / "stat").write_text("9 (x) S 1")
    assert start_time(9, proc) is None


def test_the_prefix_names_this_process_and_its_start_time() -> None:
    """Both numbers are in the name, so a later worker can tell whose directory it is."""
    prefix = spill_prefix()
    assert prefix.startswith(f"lakana-spill-{os.getpid()}-") and prefix.endswith("-")
    if sys.platform.startswith("linux"):
        assert not prefix.endswith("-0-")


def test_a_directory_of_a_dead_process_is_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nobody owns it any more; one line says how many and how much."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    gone = _spill(temp, "lakana-spill-4242-100-abcd1234", size=3 * 1024 * 1024)
    with caplog.at_level(logging.INFO):
        assert sweep_spill_directories(temp, _proc(tmp_path, {}), NOW) == 1
    assert not gone.exists()
    assert caplog.messages == [
        "Removed 1 spill directory left by a worker that stopped (3 MB freed)."
    ]


def test_a_directory_of_a_live_process_is_kept(tmp_path: Path) -> None:
    """The id exists and started at the recorded time: it is somebody's working directory."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    mine = _spill(temp, "lakana-spill-4242-100-abcd1234", age=10 * MAX_AGE_SECONDS)
    assert sweep_spill_directories(temp, _proc(tmp_path, {4242: 100}), NOW) == 0
    assert mine.exists()


def test_a_reused_process_id_with_another_start_time_is_removed(tmp_path: Path) -> None:
    """A different process now has the id: the directory's owner is gone."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    stale = _spill(temp, "lakana-spill-4242-100-abcd1234")
    assert sweep_spill_directories(temp, _proc(tmp_path, {4242: 999}), NOW) == 1
    assert not stale.exists()


def test_an_unowned_directory_goes_by_age(tmp_path: Path) -> None:
    """Without an owner record, a day decides: young is kept, old is removed."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    young = _spill(temp, "lakana-spill-abcd1234", age=60)
    old = _spill(temp, "lakana-spill-wxyz5678", age=MAX_AGE_SECONDS + 60)
    assert sweep_spill_directories(temp, _proc(tmp_path, {}), NOW) == 1
    assert young.exists() and not old.exists()


def test_without_proc_every_directory_goes_by_age(tmp_path: Path) -> None:
    """Where the owner cannot be checked, an owned directory is not removed while it is young."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    young = _spill(temp, "lakana-spill-4242-100-abcd1234", age=60)
    old = _spill(temp, "lakana-spill-4243-100-abcd1234", age=MAX_AGE_SECONDS + 60)
    assert sweep_spill_directories(temp, tmp_path / "no-proc", NOW) == 1
    assert young.exists() and not old.exists()


def test_a_symlink_named_like_a_spill_directory_is_not_followed(tmp_path: Path) -> None:
    """The target of a link keeps its files, and so does the link."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    target = tmp_path / "precious"
    target.mkdir()
    (target / "data").write_text("keep")
    link = temp / "lakana-spill-4242-100-abcd1234"
    link.symlink_to(target, target_is_directory=True)
    assert sweep_spill_directories(temp, _proc(tmp_path, {}), NOW) == 0
    assert (target / "data").read_text() == "keep" and link.is_symlink()


def test_only_exact_names_directly_under_the_temp_directory_are_touched(tmp_path: Path) -> None:
    """Other names, plain files and nested directories are left alone."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    other = _spill(temp, "lakana-spill-notes", age=10 * MAX_AGE_SECONDS)
    plain = temp / "lakana-spill-4242-100-abcd1234"
    plain.write_text("a file")
    nested_parent = temp / "inner"
    nested_parent.mkdir()
    nested = _spill(nested_parent, "lakana-spill-4242-100-abcd1234")
    assert sweep_spill_directories(temp, _proc(tmp_path, {}), NOW) == 0
    assert other.exists() and plain.exists() and nested.exists()


def test_a_missing_temp_directory_is_nothing_to_do(tmp_path: Path) -> None:
    """No temp directory, no error."""
    assert sweep_spill_directories(tmp_path / "absent", _proc(tmp_path, {}), NOW) == 0


def test_the_default_arguments_sweep_the_real_temp_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unpatched seams: the temp directory comes from `tempfile`, the clock from `time`."""
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    old = _spill(tmp_path, "lakana-spill-abcd1234", age=0)
    stamp = time.time() - MAX_AGE_SECONDS - 100
    os.utime(old, (stamp, stamp))
    assert sweep_spill_directories() == 1
    assert not old.exists()


def test_a_directory_that_vanishes_while_it_is_judged_is_not_removed(tmp_path: Path) -> None:
    """A race with its owner's own cleanup is not an error."""
    from lakana_worker.spill import _abandoned

    assert _abandoned(tmp_path / "lakana-spill-abcd1234", tmp_path / "proc", NOW) is False


def test_a_file_that_vanishes_while_sizing_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The freed-bytes count is best effort: a listed file that is gone counts for nothing."""
    from lakana_worker import spill

    (tmp_path / "real").write_bytes(b"x" * 10)
    monkeypatch.setattr(
        spill.os, "walk", lambda *a, **k: iter([(str(tmp_path), [], ["gone", "real"])])
    )
    assert spill._size(tmp_path) == 10


def test_a_directory_that_cannot_be_removed_is_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If it is still there afterwards, nothing is claimed freed."""
    from lakana_worker import spill

    temp = tmp_path / "tmp"
    temp.mkdir()
    stuck = _spill(temp, "lakana-spill-4242-100-abcd1234")
    monkeypatch.setattr(spill.shutil, "rmtree", lambda *a, **k: None)
    assert sweep_spill_directories(temp, _proc(tmp_path, {}), NOW) == 0
    assert stuck.exists()
