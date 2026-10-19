"""Tests for the local-mode data directory."""

from __future__ import annotations

import base64
import errno
import os
import stat
from pathlib import Path

import pytest

from lakana.cli._state import CliError
from lakana.cli.local import _datadir
from lakana.cli.local._datadir import (
    DataDir,
    default_data_dir,
    load_or_create_master_key,
    prepare_data_dir,
    resolve_data_dir,
)


def _mode(path: Path) -> int:
    """Return a path's permission bits.

    Parameters
    ----------
    path
        The path.

    Returns
    -------
    int
        `stat.S_IMODE` of its mode.
    """
    return stat.S_IMODE(path.stat().st_mode)


def test_default_data_dir_follows_xdg(tmp_path: Path) -> None:
    """`$XDG_DATA_HOME/lakana/local`, else `~/.local/share/lakana/local`."""
    assert default_data_dir({"XDG_DATA_HOME": str(tmp_path)}) == tmp_path / "lakana" / "local"
    assert default_data_dir({}) == Path.home() / ".local" / "share" / "lakana" / "local"


def test_prepare_creates_missing_parents_and_a_0700_leaf(tmp_path: Path) -> None:
    """Review focus 5: parents are created; only the leaf is forced to 0700."""
    data_dir = prepare_data_dir(tmp_path / "a" / "b" / "local")

    assert data_dir.root.is_dir()
    assert _mode(data_dir.root) == 0o700
    assert data_dir.lake.is_dir()


def test_prepare_resolves_a_relative_data_dir_to_an_absolute_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative `--data-dir` yields absolute URLs.

    A cwd-relative storage root or database URL would only resolve from the
    one directory the first run happened to start in, permanently breaking
    the environment for any other cwd.
    """
    monkeypatch.chdir(tmp_path)

    data_dir = prepare_data_dir(Path("data"))

    assert data_dir.root == tmp_path / "data"
    assert data_dir.root.is_absolute()
    assert data_dir.database_url == f"sqlite:///{tmp_path}/data/lakana.db"
    assert data_dir.env_root("dev") == f"file://{tmp_path}/data/lake/dev"


def test_resolve_data_dir_expands_user_and_makes_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resolve_data_dir` expands `~` and resolves relative segments."""
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = resolve_data_dir(Path("~") / "local")

    assert resolved == tmp_path / "local"
    assert resolved.is_absolute()


def test_prepare_accepts_an_existing_private_directory(tmp_path: Path) -> None:
    """An existing 0700 directory (even non-empty) is used as-is."""
    root = tmp_path / "local"
    root.mkdir(mode=0o700)
    (root / "notes.txt").write_text("x")

    assert prepare_data_dir(root).root == root


def test_prepare_refuses_a_group_accessible_directory(tmp_path: Path) -> None:
    """A 0755 data directory is refused and left unchanged."""
    root = tmp_path / "local"
    root.mkdir()
    os.chmod(root, 0o755)

    with pytest.raises(CliError, match="chmod 700") as caught:
        prepare_data_dir(root)

    assert caught.value.exit_code == 2
    assert _mode(root) == 0o755


def test_prepare_refuses_a_symlink(tmp_path: Path) -> None:
    """A symlinked data directory is refused."""
    target = tmp_path / "real"
    target.mkdir(mode=0o700)
    link = tmp_path / "local"
    link.symlink_to(target)

    with pytest.raises(CliError, match="symlink"):
        prepare_data_dir(link)


def test_prepare_refuses_a_directory_owned_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data directory this user doesn't own is refused, even at mode 0700."""
    root = tmp_path / "local"
    root.mkdir(mode=0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(_datadir.os, "getuid", lambda: real_uid + 1)

    with pytest.raises(CliError, match="owned") as caught:
        prepare_data_dir(root)

    assert caught.value.exit_code == 2


def test_prepare_refuses_an_other_writable_parent_without_sticky_bit(tmp_path: Path) -> None:
    """A 0777 parent directory without the sticky bit is refused."""
    parent = tmp_path / "parent"
    parent.mkdir()
    os.chmod(parent, 0o777)

    with pytest.raises(CliError, match="sticky") as caught:
        prepare_data_dir(parent / "local")

    assert caught.value.exit_code == 2


def test_prepare_accepts_a_group_writable_parent(tmp_path: Path) -> None:
    """Plan ruling D: a group-writable (but not other-writable) parent is fine.

    A user-private-group umask of 0002 (the Ubuntu default) routinely leaves
    a parent directory group-writable; only an *other*-writable parent
    without the sticky bit is refused.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    os.chmod(parent, 0o775)

    data_dir = prepare_data_dir(parent / "local")

    assert data_dir.root.is_dir()


def test_prepare_accepts_a_sticky_world_writable_parent(tmp_path: Path) -> None:
    """A `/tmp`-style sticky, world-writable parent is fine once ownership is checked."""
    parent = tmp_path / "parent"
    parent.mkdir()
    os.chmod(parent, 0o1777)

    data_dir = prepare_data_dir(parent / "local")

    assert data_dir.root.is_dir()


def test_prepare_refuses_a_data_dir_that_is_an_existing_file(tmp_path: Path) -> None:
    """A `--data-dir` that names an existing regular file is refused, not a traceback."""
    path = tmp_path / "local"
    path.write_text("not a directory")

    with pytest.raises(CliError) as caught:
        prepare_data_dir(path)

    assert caught.value.exit_code == 2


def test_prepare_refuses_an_unwritable_parent(tmp_path: Path) -> None:
    """A parent directory this user can't write into is refused, not a traceback."""
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o500)

    with pytest.raises(CliError) as caught:
        prepare_data_dir(parent / "local")

    assert caught.value.exit_code == 2


def test_prepare_refuses_when_the_parent_is_an_existing_file(tmp_path: Path) -> None:
    """A `--data-dir` whose parent names an existing regular file is refused."""
    parent = tmp_path / "parent"
    parent.write_text("not a directory")

    with pytest.raises(CliError, match="not a directory") as caught:
        prepare_data_dir(parent / "local")

    assert caught.value.exit_code == 2


def test_prepare_refuses_an_unwritable_grandparent(tmp_path: Path) -> None:
    """A parent that can't itself be created (its parent is unwritable) is refused."""
    grandparent = tmp_path / "grandparent"
    grandparent.mkdir(mode=0o500)

    with pytest.raises(CliError) as caught:
        prepare_data_dir(grandparent / "parent" / "local")

    assert caught.value.exit_code == 2


def test_prepare_refuses_lake_as_an_existing_regular_file(tmp_path: Path) -> None:
    """`lake` existing as a file is a clean `CliError`."""
    root = tmp_path / "local"
    root.mkdir(mode=0o700)
    (root / "lake").write_text("not a directory")

    with pytest.raises(CliError) as caught:
        prepare_data_dir(root)

    assert caught.value.exit_code == 3
    assert str(root / "lake") in str(caught.value)


def test_an_os_link_failure_creating_the_master_key_is_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.link` can fail with more than `FileExistsError`.

    Filesystems without hard-link support (some FUSE, SMB or exFAT mounts)
    raise `EPERM`/`ENOTSUP` there, a plausible `--data-dir` location.
    """
    data_dir = prepare_data_dir(tmp_path / "local")

    def boom(src: object, dst: object) -> None:
        """Simulate a filesystem that doesn't support hard links.

        Parameters
        ----------
        src
            The temp file (unused).
        dst
            The target path (unused).

        Raises
        ------
        OSError
            Always.
        """
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(_datadir.os, "link", boom)

    with pytest.raises(CliError, match="Can't create master key") as caught:
        load_or_create_master_key(data_dir)

    assert caught.value.exit_code == 3


def test_paths_and_urls(tmp_path: Path) -> None:
    """The data directory exposes its fixed layout."""
    data_dir = DataDir(tmp_path)

    assert data_dir.db_path == tmp_path / "lakana.db"
    assert data_dir.database_url == f"sqlite:///{tmp_path}/lakana.db"
    assert data_dir.env_root("dev") == f"file://{tmp_path}/lake/dev"
    assert data_dir.server_pid == tmp_path / "server.pid"
    assert data_dir.bootstrap_token_path == tmp_path / "bootstrap-token"
    assert data_dir.worker_pid == tmp_path / "worker.pid"


def test_master_key_is_generated_once_with_0600(tmp_path: Path) -> None:
    """A base64 32-byte key, written 0600, then reused unchanged."""
    data_dir = prepare_data_dir(tmp_path / "local")

    first = load_or_create_master_key(data_dir)
    second = load_or_create_master_key(data_dir)

    assert first == second
    assert len(base64.b64decode(first)) == 32
    assert _mode(data_dir.master_key_path) == 0o600


def test_master_key_creation_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    """The atomic create-if-absent doesn't litter the data directory with temp files."""
    data_dir = prepare_data_dir(tmp_path / "local")

    load_or_create_master_key(data_dir)

    assert {p.name for p in data_dir.root.iterdir()} == {
        "lake",
        "master.key",
        ".lakana-local-instance",
    }


def test_master_key_survives_a_concurrent_creator_winning_the_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If another process wins the create race, its key is read back, not overwritten."""
    data_dir = prepare_data_dir(tmp_path / "local")
    winner_key = base64.b64encode(b"\x01" * 32).decode("ascii")

    def fake_link(src: object, dst: object) -> None:
        """Simulate another process's atomic create winning the race.

        Parameters
        ----------
        src
            The temp file this process was about to link into place.
        dst
            The target path (the master key file).

        Raises
        ------
        FileExistsError
            Always, after writing a complete, valid key as the "winner"
            would have, so the loser reads back a real key rather than the
            fixture's own half-written temp file.
        """
        data_dir.master_key_path.write_text(winner_key + "\n")
        os.chmod(data_dir.master_key_path, 0o600)
        raise FileExistsError(17, "File exists")

    monkeypatch.setattr(_datadir.os, "link", fake_link)

    key = load_or_create_master_key(data_dir)

    assert key == winner_key
    # The loser's temp file must not survive the race.
    assert {p.name for p in data_dir.root.iterdir()} == {
        "lake",
        "master.key",
        ".lakana-local-instance",
    }


def test_a_zero_byte_master_key_is_refused(tmp_path: Path) -> None:
    """A crash between create and write must not silently mint an empty key."""
    data_dir = prepare_data_dir(tmp_path / "local")
    data_dir.master_key_path.touch(mode=0o600)

    with pytest.raises(CliError, match="not a valid master key") as caught:
        load_or_create_master_key(data_dir)

    assert caught.value.exit_code == 3


def test_a_garbage_master_key_is_refused(tmp_path: Path) -> None:
    """Non-base64, or the wrong decoded length, is refused rather than used."""
    data_dir = prepare_data_dir(tmp_path / "local")
    data_dir.master_key_path.write_text("not base64 at all!!\n")
    os.chmod(data_dir.master_key_path, 0o600)

    with pytest.raises(CliError, match="not a valid master key"):
        load_or_create_master_key(data_dir)


def test_a_wrong_length_master_key_is_refused(tmp_path: Path) -> None:
    """Valid base64 that doesn't decode to exactly 32 bytes is refused."""
    data_dir = prepare_data_dir(tmp_path / "local")
    data_dir.master_key_path.write_text(base64.b64encode(b"short").decode("ascii") + "\n")
    os.chmod(data_dir.master_key_path, 0o600)

    with pytest.raises(CliError, match="not a valid master key"):
        load_or_create_master_key(data_dir)


def test_a_symlinked_master_key_is_refused(tmp_path: Path) -> None:
    """A master key file that is actually a symlink is refused."""
    data_dir = prepare_data_dir(tmp_path / "local")
    real = tmp_path / "elsewhere.key"
    real.write_text(base64.b64encode(b"\x02" * 32).decode("ascii") + "\n")
    data_dir.master_key_path.symlink_to(real)

    with pytest.raises(CliError, match="symlink"):
        load_or_create_master_key(data_dir)


def test_a_dangling_symlinked_master_key_gives_a_clean_error(tmp_path: Path) -> None:
    """A dangling symlink at the key path is refused, not a raw traceback."""
    data_dir = prepare_data_dir(tmp_path / "local")
    data_dir.master_key_path.symlink_to(tmp_path / "does-not-exist")

    with pytest.raises(CliError, match="symlink"):
        load_or_create_master_key(data_dir)


def test_a_group_readable_master_key_is_refused(tmp_path: Path) -> None:
    """A 0644 master key is refused, with a chmod hint."""
    data_dir = prepare_data_dir(tmp_path / "local")
    data_dir.master_key_path.write_text(base64.b64encode(b"\x03" * 32).decode("ascii") + "\n")
    os.chmod(data_dir.master_key_path, 0o644)

    with pytest.raises(CliError, match="chmod 600"):
        load_or_create_master_key(data_dir)


def test_a_master_key_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A master key this user doesn't own is refused even if the mode looks fine."""
    data_dir = prepare_data_dir(tmp_path / "local")
    data_dir.master_key_path.write_text(base64.b64encode(b"\x04" * 32).decode("ascii") + "\n")
    os.chmod(data_dir.master_key_path, 0o600)
    real_uid = os.getuid()
    monkeypatch.setattr(_datadir.os, "getuid", lambda: real_uid + 1)

    with pytest.raises(CliError, match="owned"):
        load_or_create_master_key(data_dir)


def test_prepare_refuses_a_data_dir_whose_ancestor_is_a_regular_file(tmp_path: Path) -> None:
    """Plan ruling C: an ancestor (not just the immediate parent) being a file is refused.

    `Path.mkdir(parents=True)` raises `NotADirectoryError` here, not
    `FileExistsError` (that's the immediate-parent case, already covered) --
    this must surface as a clean `CliError` naming the path, not a
    traceback.
    """
    ancestor = tmp_path / "not_a_directory"
    ancestor.write_text("x")

    with pytest.raises(CliError) as caught:
        prepare_data_dir(ancestor / "parent" / "local")

    assert caught.value.exit_code == 3
    assert str(ancestor / "parent" / "local") in str(caught.value)


def test_a_master_key_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    """Plan ruling C: `master.key` being a directory raises `IsADirectoryError`.

    Opening it succeeds (a directory can be opened `O_RDONLY`) and its
    ownership/mode both look fine, so the failure only surfaces once the
    content is read -- it must still become a clean `CliError`, not a
    traceback. This is an access failure, not
    a corrupt key, so it must NOT carry the "delete the data directory to
    rotate it" advice -- deleting the key over a directory in its place
    loses nothing, but the message shouldn't suggest every read failure
    means the secret itself is bad.
    """
    data_dir = prepare_data_dir(tmp_path / "local")
    data_dir.master_key_path.mkdir(mode=0o700)

    with pytest.raises(CliError, match="Can't read") as caught:
        load_or_create_master_key(data_dir)

    assert caught.value.exit_code == 3
    assert "rotate" not in str(caught.value)


def test_a_zero_mode_master_key_is_refused_without_rotate_advice(tmp_path: Path) -> None:
    """A `PermissionError` opening the key isn't "corrupt"."""
    data_dir = prepare_data_dir(tmp_path / "local")
    data_dir.master_key_path.write_text(base64.b64encode(b"\x05" * 32).decode("ascii") + "\n")
    os.chmod(data_dir.master_key_path, 0o000)

    with pytest.raises(CliError, match="Can't read") as caught:
        load_or_create_master_key(data_dir)

    assert caught.value.exit_code == 3
    assert "rotate" not in str(caught.value)


def test_prepare_reports_an_unexpected_os_error_creating_the_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan ruling C: an `OSError` creating the leaf itself is also a clean `CliError`.

    The parent already exists, so `path.parent.mkdir(exist_ok=True)` never
    even reaches `os.mkdir` for a path this fake distinguishes -- only the
    leaf's own `os.mkdir` call is made to fail with something other than
    `FileExistsError`/`PermissionError`.
    """
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "local"
    real_mkdir = _datadir.os.mkdir

    def selective(path: Path, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        """Fail only the leaf's own `mkdir` call; delegate everything else.

        Parameters
        ----------
        path
            The path `os.mkdir` was asked to create.
        mode
            The requested mode.
        dir_fd
            Passed through unchanged.

        Raises
        ------
        OSError
            When `path` is the leaf being prepared.
        """
        if path == target:
            raise OSError("simulated I/O error creating the leaf")
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(_datadir.os, "mkdir", selective)

    with pytest.raises(CliError) as caught:
        prepare_data_dir(target)

    assert caught.value.exit_code == 3
    assert str(target) in str(caught.value)


def test_a_non_utf8_master_key_is_refused(tmp_path: Path) -> None:
    """Plan ruling C: non-UTF-8 bytes in `master.key` are refused, not a traceback."""
    data_dir = prepare_data_dir(tmp_path / "local")
    data_dir.master_key_path.write_bytes(b"\xff\xfe\x00not utf8 at all")
    os.chmod(data_dir.master_key_path, 0o600)

    with pytest.raises(CliError, match="not a valid master key") as caught:
        load_or_create_master_key(data_dir)

    assert caught.value.exit_code == 3
