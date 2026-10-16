"""Tests for locating the server console scripts."""

from __future__ import annotations

import sysconfig
from dataclasses import dataclass
from pathlib import Path

import pytest

from lakana.cli._state import CliError
from lakana.cli.local import _scripts


def test_finds_installed_scripts_in_this_environment() -> None:
    """In the dev workspace both scripts are installed next to the interpreter."""
    for name in ("lakana-server", "lakana-worker"):
        path = _scripts.find_script(name)
        assert path.exists()
        assert path.name.startswith(name)


def test_a_missing_entry_point_asks_for_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """No registered entry point → exit 3 with the install hint."""
    monkeypatch.setattr(_scripts, "entry_points", lambda **kw: [])

    with pytest.raises(CliError, match=r'pip install "lakana\[server\]"') as caught:
        _scripts.find_script("lakana-server")

    assert caught.value.exit_code == 3


def test_a_registered_but_missing_file_asks_for_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry point whose script file isn't in the scripts dir → exit 3."""
    monkeypatch.setattr(_scripts, "entry_points", lambda **kw: [_FakeEntryPoint(dist=None)])
    monkeypatch.setattr(_scripts, "_scripts_dir", lambda: tmp_path)

    with pytest.raises(CliError) as caught:
        _scripts.find_script("lakana-server")

    assert caught.value.exit_code == 3


class _FakePackagePath:
    """A stand-in for `importlib.metadata.PackagePath` in a fake distribution.

    Parameters
    ----------
    raw
        The forward-slash-separated relative path, as `Distribution.files`
        entries are recorded (e.g. `"../../../bin/lakana-server"`).
    """

    def __init__(self, raw: str) -> None:
        self.parts = tuple(raw.split("/"))
        self.name = self.parts[-1]


class _FakeDistribution:
    """A stand-in for `importlib.metadata.Distribution` naming one script file.

    Parameters
    ----------
    entry
        The `Distribution.files` entry naming the script.
    target
        The real path `locate_file` resolves that entry to.
    """

    def __init__(self, entry: _FakePackagePath, target: Path) -> None:
        self.files = [entry]
        self._target = target

    def locate_file(self, path: _FakePackagePath) -> Path:
        """Resolve a recorded file entry to its real location.

        Parameters
        ----------
        path
            One of `self.files`.

        Returns
        -------
        Path
            The fixed target this fake was built with.
        """
        return self._target


@dataclass
class _FakeEntryPoint:
    """A stand-in for `importlib.metadata.EntryPoint` carrying a fake distribution.

    Parameters
    ----------
    dist
        The distribution this entry point belongs to.
    """

    dist: object | None


def test_resolves_through_the_entry_points_distribution_file_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dist.files`/`locate_file` wins over the interpreter's scripts dir.

    This is what makes `find_script` correct under `pip install --user` (a
    separate scripts directory) and prevents it from silently picking up
    another installation's same-named script from `sysconfig`'s directory.
    """
    real_bin = tmp_path / "elsewhere" / "bin"
    real_bin.mkdir(parents=True)
    script = real_bin / "lakana-server"
    script.write_text("#!/bin/sh\n")

    entry = _FakePackagePath("../../../elsewhere/bin/lakana-server")
    dist = _FakeDistribution(entry, script)
    monkeypatch.setattr(_scripts, "entry_points", lambda **kw: [_FakeEntryPoint(dist=dist)])
    monkeypatch.setattr(_scripts, "_scripts_dir", lambda: tmp_path / "wrong-directory")

    assert _scripts.find_script("lakana-server") == script


def test_scripts_dir_matches_sysconfig() -> None:
    """`_scripts_dir` is exactly `sysconfig`'s console-script directory."""
    assert _scripts._scripts_dir() == Path(sysconfig.get_path("scripts"))


class _NoFilesDistribution:
    """A stand-in distribution with no recorded file list (e.g. an egg-link install)."""

    files = None


def test_falls_back_to_sysconfig_when_the_distribution_has_no_file_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A distribution with no `files` at all falls back to the scripts directory."""
    monkeypatch.setattr(
        _scripts, "entry_points", lambda **kw: [_FakeEntryPoint(dist=_NoFilesDistribution())]
    )
    monkeypatch.setattr(_scripts, "_scripts_dir", lambda: tmp_path)
    script = tmp_path / "lakana-server"
    script.write_text("#!/bin/sh\n")

    assert _scripts.find_script("lakana-server") == script


def test_falls_back_to_sysconfig_when_no_recorded_file_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A distribution whose file list doesn't name this script falls back to sysconfig."""
    entry = _FakePackagePath("../../../bin/some-other-tool")
    dist = _FakeDistribution(entry, tmp_path / "unused")
    monkeypatch.setattr(_scripts, "entry_points", lambda **kw: [_FakeEntryPoint(dist=dist)])
    monkeypatch.setattr(_scripts, "_scripts_dir", lambda: tmp_path)
    script = tmp_path / "lakana-server"
    script.write_text("#!/bin/sh\n")

    assert _scripts.find_script("lakana-server") == script
