"""A test that leaves a read-only tree must not stop pytest from rotating its temp directories.

pytest keeps the last three ``pytest-N`` directories under ``/tmp/pytest-of-$USER`` and deletes
older ones at the start of a run. It cannot delete a read-only tree, so the code-unpack tests
 filled a RAM disk with directories nothing could remove.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from scripts.pytest_writable_tmp import make_tree_writable

REPO = Path(__file__).resolve().parents[2]

INNER_TEST = """
import os
from pathlib import Path


def test_leaves_a_read_only_tree(tmp_path):
    tree = tmp_path / "code" / "sub"
    tree.mkdir(parents=True)
    (tree / "a.py").write_text("x = 1\\n")
    (tree / "a.py").chmod(0o444)
    tree.chmod(0o555)
    (tmp_path / "code").chmod(0o555)
    Path(os.environ["INNER_RECORD"]).write_text(str(tmp_path / "code"))
"""


def _run_inner(tmp_path: Path, conftest: str) -> Path:
    """Run a pytest session in a scratch directory and return the tree its test left.

    Parameters
    ----------
    tmp_path
        The scratch directory for the inner session.
    conftest
        The text of the inner ``conftest.py``.

    Returns
    -------
    Path
        The read-only tree the inner test created.
    """
    inner = tmp_path / "inner"
    inner.mkdir(parents=True)
    (inner / "conftest.py").write_text(conftest)
    (inner / "test_inner.py").write_text(INNER_TEST)
    record = tmp_path / "record.txt"
    env = {
        **os.environ,
        "INNER_RECORD": str(record),
        "PYTHONPATH": str(REPO),
        "PYTEST_ADDOPTS": "",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "base"),
            str(inner),
        ],
        cwd=inner,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return Path(record.read_text())


def _writable(path: Path) -> bool:
    """Tell whether the owner can write to every directory and file below `path`.

    Parameters
    ----------
    path
        The tree.

    Returns
    -------
    bool
        True when no entry lacks the owner's write bit.
    """
    entries = [path, *path.rglob("*")]
    return all(p.lstat().st_mode & stat.S_IWUSR for p in entries)


def test_a_read_only_tree_is_writable_again_after_the_session(tmp_path: Path) -> None:
    left = _run_inner(
        tmp_path / "with",
        "pytest_plugins = ['scripts.pytest_writable_tmp']\n",
    )
    assert _writable(left)


def test_without_the_plugin_the_tree_stays_read_only(tmp_path: Path) -> None:
    """Guards the test above: the inner test really does leave a tree pytest cannot delete."""
    left = _run_inner(tmp_path / "without", "")
    try:
        assert not _writable(left)
    finally:
        make_tree_writable(left.parent)


def test_make_tree_writable_opens_files_and_directories_and_ignores_links(tmp_path: Path) -> None:
    tree = tmp_path / "t"
    (tree / "d").mkdir(parents=True)
    (tree / "d" / "f").write_text("x")
    (tree / "d" / "f").chmod(0o444)
    (tree / "d" / "link").symlink_to(tmp_path / "missing")
    (tree / "d").chmod(0o555)
    tree.chmod(0o555)
    make_tree_writable(tree)
    assert _writable(tree)
    make_tree_writable(tmp_path / "does-not-exist")


def test_make_tree_writable_opens_a_single_file(tmp_path: Path) -> None:
    single = tmp_path / "f"
    single.write_text("x")
    single.chmod(0o444)
    make_tree_writable(single)
    assert single.stat().st_mode & stat.S_IWUSR


def test_make_tree_writable_leaves_a_root_link_and_its_target_alone(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.write_text("x")
    target.chmod(0o444)
    (tmp_path / "root").symlink_to(target)
    try:
        make_tree_writable(tmp_path / "root")
        assert not target.stat().st_mode & stat.S_IWUSR
    finally:
        target.chmod(0o644)
