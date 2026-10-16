"""A pytest plugin: make every temp directory writable again when the session ends.

pytest keeps the last three ``pytest-N`` directories under ``/tmp/pytest-of-$USER`` and removes
older ones when the next run starts. It cannot remove a tree whose directories or files lack the
owner's write bit, and some tests leave exactly that behind on purpose: the worker unpacks
project code read-only (mode 0555 and 0444), the artifact-tree tests check sealed copies. The
directories then piled up until a RAM disk ran out of inodes and every later run failed with
``could not create numbered dir``.

Loaded for every run from ``packages/conftest.py`` and ``scripts/conftest.py``. The test
that leaves the tree needs no cleanup of its own; a session fixture opens everything below the
session's base temp directory at the end. A run that is killed does not get there; the
manual fix is ``chmod -R u+w /tmp/pytest-of-$USER``.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest


def make_tree_writable(path: Path) -> None:
    """Give the owner write (and, for directories, search) permission on a whole tree.

    Symbolic links are neither followed nor changed, so a link planted by a test cannot make
    this touch anything outside the tree. A path that does not exist is ignored.

    Parameters
    ----------
    path
        The root of the tree.
    """
    try:
        root_mode = path.lstat().st_mode
    except OSError:
        return
    if stat.S_ISLNK(root_mode):
        return
    if stat.S_ISDIR(root_mode):
        path.chmod(root_mode | stat.S_IRWXU)
        for current, directories, files in os.walk(path):
            for name in directories:
                _open_entry(Path(current) / name, stat.S_IRWXU)
            for name in files:
                _open_entry(Path(current) / name, stat.S_IWUSR)
    else:
        _open_entry(path, stat.S_IWUSR)


def _open_entry(path: Path, bits: int) -> None:
    """Add permission bits to one entry unless it is a symbolic link.

    Parameters
    ----------
    path
        The entry.
    bits
        The bits to add to its mode.
    """
    mode = path.lstat().st_mode
    if not stat.S_ISLNK(mode):
        path.chmod(mode | bits)


@pytest.fixture(scope="session", autouse=True)
def _writable_temp_trees(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Make everything below the session's base temp directory writable at the end.

    Parameters
    ----------
    tmp_path_factory
        Names the base temp directory of this session (of this worker under pytest-xdist).

    Yields
    ------
    None
        Once; the work happens after the session's last test.
    """
    yield
    make_tree_writable(tmp_path_factory.getbasetemp())
