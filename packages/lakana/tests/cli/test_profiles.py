"""Tests for writing the profile file."""

from __future__ import annotations

import os
import stat
import threading
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import write_profile_file

from lakana._config import resolve_config
from lakana.cli import _profiles
from lakana.cli._filelock import try_locked
from lakana.cli._state import CliError
from lakana.errors import ConfigError


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


def test_set_profile_creates_a_0600_file_in_a_0700_directory(tmp_path: Path) -> None:
    """A first login creates the directory and file with private permissions."""
    path = tmp_path / "cfg" / "lakana" / "config.toml"

    _profiles.set_profile(
        path, "default", url="http://u", token="lakana_pat_k_s", default_environment="dev"
    )

    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert tomllib.loads(path.read_text()) == {
        "default_profile": "default",
        "profiles": {
            "default": {"url": "http://u", "token": "lakana_pat_k_s", "default_environment": "dev"}
        },
    }


def test_the_written_file_round_trips_through_the_sdk(tmp_path: Path) -> None:
    """What `login` writes, the SDK's own `resolve_config` reads unchanged."""
    path = tmp_path / "lakana" / "config.toml"
    _profiles.set_profile(
        path, "work", url="https://p.example", token="lakana_pat_k_s", default_environment=None
    )

    config = resolve_config(config_path=path, environ={})

    assert (config.url, config.token) == ("https://p.example", "lakana_pat_k_s")


@pytest.mark.parametrize(
    "name",
    ["default", "with.dot", "with space", 'quo"te', "ümlaut", "back\\slash", "has\x7fdel"],
)
def test_awkward_names_and_values_round_trip(tmp_path: Path, name: str) -> None:
    """Review focus 1: quotes, dots, spaces and non-ASCII survive the emitter."""
    path = tmp_path / "lakana" / "config.toml"
    url = f'http://h/{name}?q="1"'

    _profiles.set_profile(path, name, url=url, token="lakana_pat_k_s", default_environment=name)

    data = tomllib.loads(path.read_text())
    assert data["profiles"][name] == {
        "url": url,
        "token": "lakana_pat_k_s",
        "default_environment": name,
    }
    config = resolve_config(profile=name, config_path=path, environ={})
    assert config.url == url


def test_set_profile_preserves_other_profiles_and_unknown_keys(tmp_path: Path) -> None:
    """Rewriting one profile keeps every other profile and key intact."""
    path = tmp_path / "lakana" / "config.toml"
    write_profile_file(
        path,
        'default_profile = "a"\nextra = "keep"\n'
        '[profiles.a]\nurl = "http://a"\ntoken = "ta"\ncolor = "blue"\n'
        '[profiles.b]\nurl = "http://b"\ntoken = "tb"\n',
    )

    _profiles.set_profile(path, "b", url="http://b2", token="tb2", default_environment=None)

    data = tomllib.loads(path.read_text())
    assert data["default_profile"] == "a"
    assert data["extra"] == "keep"
    assert data["profiles"]["a"] == {"url": "http://a", "token": "ta", "color": "blue"}
    assert data["profiles"]["b"] == {"url": "http://b2", "token": "tb2"}


def test_set_profile_keeps_an_existing_default_environment_when_given_none(
    tmp_path: Path,
) -> None:
    """Re-login without choosing an environment doesn't erase the old default."""
    path = tmp_path / "lakana" / "config.toml"
    _profiles.set_profile(path, "p", url="u", token="t", default_environment="dev")

    _profiles.set_profile(path, "p", url="u", token="t2", default_environment=None)

    assert tomllib.loads(path.read_text())["profiles"]["p"]["default_environment"] == "dev"


def test_a_permissive_existing_file_is_refused_and_left_alone(tmp_path: Path) -> None:
    """The CLI never rewrites (or re-permissions) a file the SDK would refuse."""
    path = tmp_path / "lakana" / "config.toml"
    write_profile_file(path, 'default_profile = "a"\n')
    os.chmod(path, 0o644)

    with pytest.raises(ConfigError, match="chmod 600"):
        _profiles.set_profile(path, "a", url="u", token="t", default_environment=None)

    assert _mode(path) == 0o644
    assert path.read_text() == 'default_profile = "a"\n'


def test_a_symlinked_profile_file_is_refused(tmp_path: Path) -> None:
    """A symlink at the profile path is refused, not followed."""
    target = tmp_path / "elsewhere.toml"
    write_profile_file(target, "")
    path = tmp_path / "lakana" / "config.toml"
    path.parent.mkdir(mode=0o700)
    path.symlink_to(target)

    with pytest.raises(ConfigError, match="symlink"):
        _profiles.set_profile(path, "a", url="u", token="t", default_environment=None)


def test_a_permissive_existing_directory_is_refused_and_left_alone(tmp_path: Path) -> None:
    """A pre-existing, group/other-accessible parent directory is refused, not re-permissioned."""
    path = tmp_path / "lakana" / "config.toml"
    path.parent.mkdir()
    os.chmod(path.parent, 0o777)

    with pytest.raises(ConfigError, match="chmod 700"):
        _profiles.set_profile(path, "a", url="u", token="t", default_environment=None)

    assert _mode(path.parent) == 0o777
    assert not path.exists()


def test_a_symlinked_parent_directory_is_refused(tmp_path: Path) -> None:
    """A symlink standing in for the profile directory is refused, not followed."""
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    path = tmp_path / "lakana" / "config.toml"
    path.parent.symlink_to(target)

    with pytest.raises(ConfigError, match="symlink"):
        _profiles.set_profile(path, "a", url="u", token="t", default_environment=None)


def test_directory_checks_are_skipped_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows, neither the symlink nor the permission-bit check on the directory runs.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Used to make `save` believe it is running on Windows.
    """
    path = tmp_path / "lakana" / "config.toml"
    path.parent.mkdir()
    os.chmod(path.parent, 0o777)
    monkeypatch.setattr(_profiles.os, "name", "nt")

    _profiles.set_profile(path, "a", url="u", token="t", default_environment=None)

    assert tomllib.loads(path.read_text())["profiles"]["a"]["url"] == "u"


def test_set_profile_replaces_an_empty_default_profile(tmp_path: Path) -> None:
    """A file with `default_profile = ""` gets the new name, not the stale empty string."""
    path = tmp_path / "lakana" / "config.toml"
    write_profile_file(path, 'default_profile = ""\n')

    _profiles.set_profile(path, "a", url="u", token="t", default_environment=None)

    assert tomllib.loads(path.read_text())["default_profile"] == "a"


def test_a_non_string_value_is_refused_rather_than_dropped(tmp_path: Path) -> None:
    """The flat emitter can't represent nested tables/numbers; it refuses rather than lose data."""
    path = tmp_path / "lakana" / "config.toml"
    write_profile_file(path, 'timeout = 5\n[profiles.a]\nurl = "u"\ntoken = "t"\n')

    with pytest.raises(ConfigError, match="timeout"):
        _profiles.set_profile(path, "a", url="u", token="t2", default_environment=None)


def test_no_temp_file_is_left_behind(tmp_path: Path) -> None:
    """The atomic write leaves only `config.toml` in the directory."""
    path = tmp_path / "lakana" / "config.toml"
    _profiles.set_profile(path, "a", url="u", token="t", default_environment=None)

    assert sorted(p.name for p in path.parent.iterdir()) == ["config.toml", "config.toml.lock"]


def test_remove_profile_clears_default_profile_when_it_was_the_default(tmp_path: Path) -> None:
    """Removing the default profile also removes `default_profile`."""
    path = tmp_path / "lakana" / "config.toml"
    _profiles.set_profile(path, "a", url="u", token="t", default_environment=None)
    _profiles.set_profile(path, "b", url="u", token="t", default_environment=None)

    _profiles.remove_profile(path, "a")

    data = tomllib.loads(path.read_text())
    assert "default_profile" not in data
    assert list(data["profiles"]) == ["b"]


def test_remove_profile_refuses_an_unknown_name(tmp_path: Path) -> None:
    """Removing a profile that doesn't exist is a usage error."""
    with pytest.raises(CliError, match="No profile named 'nope'"):
        _profiles.remove_profile(tmp_path / "lakana" / "config.toml", "nope")


def test_list_profiles_never_includes_tokens(tmp_path: Path) -> None:
    """`ProfileSummary` has no token field at all."""
    path = tmp_path / "lakana" / "config.toml"
    _profiles.set_profile(
        path, "a", url="http://a", token="lakana_pat_k_secret", default_environment="dev"
    )

    [summary] = _profiles.list_profiles(path)

    assert summary == _profiles.ProfileSummary(
        name="a", url="http://a", default_environment="dev", is_default=True
    )
    assert "secret" not in repr(summary)


def test_dumps_refuses_a_non_table_profiles_value() -> None:
    """`profiles` must be a table; anything else is refused rather than silently dropped."""
    with pytest.raises(ConfigError, match="profiles is not a table"):
        _profiles.dumps({"profiles": "nope"})


def test_dumps_refuses_a_non_table_profile_entry() -> None:
    """Each profile entry must itself be a table; a bare value is refused."""
    with pytest.raises(ConfigError, match=r"profiles\.a is not a table"):
        _profiles.dumps({"profiles": {"a": "nope"}})


def test_save_removes_the_temp_file_if_the_final_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure between writing the temp file and the atomic rename leaves nothing behind.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Used to make `os.replace` fail after the temp file is written.
    """
    path = tmp_path / "lakana" / "config.toml"

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_profiles.os, "replace", _boom)

    with pytest.raises(OSError, match="disk full"):
        _profiles.save(path, {"profiles": {"a": {"url": "u", "token": "t"}}})

    assert list(path.parent.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="directories can't be fsynced on Windows")
def test_save_fsyncs_the_parent_directory_after_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review minor 14: the rename is made durable by fsyncing the directory."""
    path = tmp_path / "lakana" / "config.toml"
    synced: list[tuple[bool, bool]] = []
    real_fsync = os.fsync

    def record(fd: int) -> None:
        synced.append((stat.S_ISDIR(os.fstat(fd).st_mode), path.exists()))
        real_fsync(fd)

    monkeypatch.setattr(_profiles.os, "fsync", record)

    _profiles.save(path, {"profiles": {"a": {"url": "u", "token": "t"}}})

    assert synced == [(False, False), (True, True)]


def test_save_skips_the_directory_fsync_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows a directory can't be opened for fsync, so only the file is synced."""
    path = tmp_path / "lakana" / "config.toml"
    path.parent.mkdir(mode=0o700)
    synced: list[bool] = []
    real_fsync = os.fsync

    def record(fd: int) -> None:
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(_profiles.os, "fsync", record)
    monkeypatch.setattr(_profiles.os, "name", "nt")

    _profiles.save(path, {"profiles": {"a": {"url": "u", "token": "t"}}})

    assert synced == [False]


@pytest.mark.parametrize(
    "text",
    ['profiles = "x"\n', "[profiles]\na = 1\n", '[profiles.a]\nurl = 5\ntoken = "t"\n'],
)
def test_writing_to_a_profile_file_of_the_wrong_shape_is_a_config_error(
    tmp_path: Path, text: str
) -> None:
    """The CLI refuses a malformed file with ConfigError (exit 3), not a crash."""
    path = tmp_path / "lakana" / "config.toml"
    write_profile_file(path, text)

    for action in (
        lambda: _profiles.set_profile(path, "b", url="u", token="t", default_environment=None),
        lambda: _profiles.list_profiles(path),
        lambda: _profiles.use_profile(path, "a", None),
        lambda: _profiles.remove_profile(path, "a"),
    ):
        with pytest.raises(ConfigError):
            action()
    assert path.read_text() == text


def _lock_path(path: Path) -> Path:
    """Return the lock file `_profiles` uses for a profile file.

    Parameters
    ----------
    path
        The profile file.

    Returns
    -------
    Path
        Its sibling lock file.
    """
    return path.with_name(path.name + ".lock")


def test_interleaved_writers_both_keep_their_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second `set_profile` that starts between a first one's load and save is not lost.

    Writer A is paused right after its load until writer B has had every
    chance to finish; the lock makes B wait, so A's save lands first and B
    then loads A's result.
    """
    path = tmp_path / "lakana" / "config.toml"
    _profiles.set_profile(path, "seed", url="http://s", token="t", default_environment=None)
    real_load = _profiles.load
    a_loaded = threading.Event()
    b_done = threading.Event()
    a_thread = threading.current_thread()

    def paused_load(target: Path) -> dict[str, Any]:
        """Load, then (for writer A only) wait for writer B to finish.

        Parameters
        ----------
        target
            The profile file.

        Returns
        -------
        dict
            The loaded document.
        """
        data = real_load(target)
        if threading.current_thread() is a_thread:
            a_loaded.set()
            b_done.wait(0.5)
        return data

    monkeypatch.setattr(_profiles, "load", paused_load)

    def writer_b() -> None:
        """Wait for A's load, then write profile `b`."""
        assert a_loaded.wait(5)
        _profiles.set_profile(path, "b", url="http://b", token="t", default_environment=None)
        b_done.set()

    thread = threading.Thread(target=writer_b)
    thread.start()
    _profiles.set_profile(path, "a", url="http://a", token="t", default_environment=None)
    thread.join(10)

    assert set(real_load(path)["profiles"]) == {"seed", "a", "b"}


def test_the_lock_file_is_a_private_sibling(tmp_path: Path) -> None:
    """Updating a profile leaves a 0600 lock file next to the profile file."""
    path = tmp_path / "lakana" / "config.toml"
    _profiles.set_profile(path, "a", url="http://a", token="t", default_environment=None)
    assert _mode(_lock_path(path)) == 0o600


@pytest.mark.parametrize(
    "update",
    [
        lambda p: _profiles.set_profile(
            p, "x", url="http://x", token="t", default_environment=None
        ),
        lambda p: _profiles.remove_profile(p, "a"),
        lambda p: _profiles.use_profile(p, "a", None),
    ],
    ids=["set", "remove", "use"],
)
def test_an_update_gives_up_after_the_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update: Callable[[Path], None]
) -> None:
    """Every writer waits a bounded time, then fails naming the other process."""
    path = tmp_path / "lakana" / "config.toml"
    _profiles.set_profile(path, "a", url="http://a", token="t", default_environment=None)
    monkeypatch.setattr(_profiles, "_LOCK_TIMEOUT", 0.1)

    with try_locked(_lock_path(path)) as held:
        assert held
        with pytest.raises(CliError, match="Another lakana process is updating"):
            update(path)

    assert set(_profiles.load(path)["profiles"]) == {"a"}
