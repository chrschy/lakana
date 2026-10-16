"""Finding `lakana.yaml` and its `repo:` from the working directory."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lakana.cli._project import find_project_root, is_dirty, load_project_yaml, project_repo
from lakana.cli._state import CliError


def _git(cwd: Path, *args: str) -> None:
    """Run git quietly with a fixed identity.

    Parameters
    ----------
    cwd
        Working directory.
    *args
        Arguments after ``git``.
    """
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_repo_is_found_from_a_subdirectory(tmp_path: Path) -> None:
    """Walking up from a subdirectory finds lakana.yaml."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "assets" / "bronze").mkdir(parents=True)
    (tmp_path / "lakana.yaml").write_text("repo: analytics\n")
    assert find_project_root(tmp_path / "assets" / "bronze") == tmp_path
    assert project_repo(tmp_path / "assets" / "bronze") == "analytics"


def test_walk_stops_at_the_git_root(tmp_path: Path) -> None:
    """A lakana.yaml above the enclosing repository is not this project's."""
    (tmp_path / "lakana.yaml").write_text("repo: outer\n")
    inner = tmp_path / "inner"
    inner.mkdir()
    _git(inner, "init", "-q")
    assert find_project_root(inner) is None
    assert project_repo(inner) is None


def test_no_repo_key_is_none(tmp_path: Path) -> None:
    """A lakana.yaml without `repo:` does not name a repository."""
    (tmp_path / "lakana.yaml").write_text("defaults: {owner: x}\n")
    assert project_repo(tmp_path) is None


def test_aliases_are_refused(tmp_path: Path) -> None:
    """The CLI's loader refuses anchors and aliases too."""
    with pytest.raises(CliError, match="anchors and aliases"):
        load_project_yaml("a: &x 1\nb: *x\n")


def test_dirty_tree_is_detected(tmp_path: Path) -> None:
    """Uncommitted changes make the tree dirty; a clean commit does not."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "lakana.yaml").write_text("repo: a\n")
    assert is_dirty(tmp_path)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    assert not is_dirty(tmp_path)


def test_not_a_git_repository_is_not_dirty(tmp_path: Path) -> None:
    """Outside git there is nothing to warn about."""
    assert not is_dirty(tmp_path)


def test_anchors_on_collections_are_refused() -> None:
    """An anchored mapping or sequence is refused, not only an anchored scalar."""
    with pytest.raises(CliError, match="anchors and aliases"):
        load_project_yaml("a: &x [1, 2]\n")


def test_oversized_file_is_refused() -> None:
    """The size cap mirrors core's loader."""
    with pytest.raises(CliError, match="larger than 64 KiB"):
        load_project_yaml("a: " + "x" * 70_000 + "\n")


def test_malformed_yaml_is_a_cli_error() -> None:
    """A parse error is a usage error, not a traceback."""
    with pytest.raises(CliError, match="not valid"):
        load_project_yaml("a: [unclosed\n")


def test_non_mapping_or_non_string_repo_is_none(tmp_path: Path) -> None:
    """Only a string `repo:` in a mapping names a repository."""
    (tmp_path / "lakana.yaml").write_text("- a\n- b\n")
    assert project_repo(tmp_path) is None
    (tmp_path / "lakana.yaml").write_text("repo: 5\n")
    assert project_repo(tmp_path) is None


def test_missing_git_is_not_dirty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the git executable is unavailable there is nothing to warn about."""

    def boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    assert not is_dirty(tmp_path)


def test_non_utf8_file_is_a_cli_error(tmp_path: Path) -> None:
    """Undecodable bytes are a usage error, not a traceback."""
    (tmp_path / "lakana.yaml").write_bytes(b"repo: \xff\xfe\n")
    with pytest.raises(CliError, match="UTF-8"):
        project_repo(tmp_path)


def test_oversized_file_is_refused_before_decoding(tmp_path: Path) -> None:
    """The cap applies to the bytes on disk, without reading the whole file."""
    (tmp_path / "lakana.yaml").write_bytes(b"\xff" * 200_000)
    with pytest.raises(CliError, match="larger than 64 KiB"):
        project_repo(tmp_path)


def test_outside_git_only_the_working_directory_is_searched(tmp_path: Path) -> None:
    """A stray lakana.yaml in a parent never names the repo for a git-less subdirectory."""
    (tmp_path / "lakana.yaml").write_text("repo: stray\n")
    child = tmp_path / "child"
    child.mkdir()
    assert find_project_root(child) is None
    assert project_repo(child) is None
    assert find_project_root(tmp_path) == tmp_path


def test_walk_climbs_inside_a_git_repository(tmp_path: Path) -> None:
    """Inside a repository the walk reaches a lakana.yaml at the git root."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "lakana.yaml").write_text("repo: a\n")
    (tmp_path / "x" / "y").mkdir(parents=True)
    assert find_project_root(tmp_path / "x" / "y") == tmp_path


def test_a_symlinked_lakana_yaml_is_not_a_project(tmp_path: Path) -> None:
    """Server discovery refuses a symlinked lakana.yaml; so does the CLI."""
    (tmp_path / "real.yaml").write_text("repo: a\n")
    (tmp_path / "lakana.yaml").symlink_to(tmp_path / "real.yaml")
    assert find_project_root(tmp_path) is None
    assert project_repo(tmp_path) is None


def test_dirty_check_disables_fsmonitor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repo-level config must not run code: git is called with core.fsmonitor=false."""
    seen: list[list[str]] = []

    def fake(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert not is_dirty(tmp_path)
    assert seen == [["git", "-c", "core.fsmonitor=false", "status", "--porcelain"]]


def test_unreadable_file_is_a_cli_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An I/O error reading lakana.yaml is a usage error."""
    (tmp_path / "lakana.yaml").write_text("repo: a\n")

    def boom(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", boom)
    with pytest.raises(CliError, match="Can't read"):
        project_repo(tmp_path)
