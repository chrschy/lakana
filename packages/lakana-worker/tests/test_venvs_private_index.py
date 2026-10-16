"""A venv built from a private index, through `acquire` and the real uv."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from index_fixture import PASSWORD, USERNAME, Index, project_files, serving

from lakana_worker.index_credentials import IndexAccess
from lakana_worker.venvs import VenvBuildError, VenvBuildRefused, VenvSettings, acquire


class Build:
    """One worker's venv cache and one unpacked project.

    Parameters
    ----------
    root
        The test's directory; everything the build touches lies under it.
    files
        The project's files by name.
    """

    def __init__(self, root: Path, files: dict[str, str]) -> None:
        uv = os.environ.get("UV") or shutil.which("uv")
        assert uv, "these tests need uv; run them with `uv run pytest`"
        self.root = root
        self.project = root / "code"
        self.project.mkdir(parents=True)
        for name, text in files.items():
            (self.project / name).write_text(text, encoding="utf-8")
        self.settings = VenvSettings(
            uv=uv, cache=root / "venvs", uv_cache=root / "uv-cache", build_timeout=60.0
        )
        self.log = bytearray()

    def acquire(self, indexes: Callable[[], list[IndexAccess]] | None) -> Path:
        """Acquire the project's venv and release it at once.

        Parameters
        ----------
        indexes
            The index resolver the worker would pass, or `None`.

        Returns
        -------
        Path
            The venv directory.
        """
        digest = hashlib.sha256((self.project / "uv.lock").read_bytes()).hexdigest()
        lease = acquire(self.settings, digest, self.project, self.log.extend, indexes)
        lease.release()
        return lease.path

    def files_holding(self, secret: str) -> list[str]:
        """List the files under the test's directory that contain a secret.

        Parameters
        ----------
        secret
            The text to look for.

        Returns
        -------
        list of str
            Paths relative to the test's directory.
        """
        needle = secret.encode()
        return [
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink() and needle in path.read_bytes()
        ]


def _team(index: Index, password: str = PASSWORD) -> list[IndexAccess]:
    """Describe the registered index with its credential.

    Parameters
    ----------
    index
        The fixture index.
    password
        The secret's password.

    Returns
    -------
    list of IndexAccess
        The index as the worker resolves it.
    """
    return [IndexAccess("team", index.url(), "dev", "team-index", USERNAME, password)]


def _never() -> list[IndexAccess]:
    """Fail the test if a secret is asked for.

    Raises
    ------
    AssertionError
        Always.
    """
    raise AssertionError("a cached environment must not read a secret")


def test_a_private_package_is_installed_and_the_credential_is_nowhere(tmp_path: Path) -> None:
    with serving() as indexes:
        build = Build(tmp_path, project_files(indexes.registered.url()))
        venv = build.acquire(lambda: _team(indexes.registered))
        assert indexes.registered.credentials_received == [f"{USERNAME}:{PASSWORD}"]
    (module,) = venv.glob("lib/*/site-packages/team_utils/__init__.py")
    assert module.read_text() == "VALUE = 42\n"
    assert PASSWORD.encode() not in bytes(build.log)
    assert build.files_holding(PASSWORD) == []


def test_a_cached_environment_reads_no_secret_and_contacts_no_index(tmp_path: Path) -> None:
    """A rotated or removed credential affects only the next build."""
    with serving() as indexes:
        build = Build(tmp_path, project_files(indexes.registered.url()))
        first = build.acquire(lambda: _team(indexes.registered))
        seen = len(indexes.registered.server.seen)
        assert build.acquire(_never) == first
        assert len(indexes.registered.server.seen) == seen


def test_a_corrected_secret_builds_without_a_new_deploy(tmp_path: Path) -> None:
    """A wrong password is a refusal; the same files build once it is right."""
    with serving() as indexes:
        build = Build(tmp_path, project_files(indexes.registered.url()))
        with pytest.raises(VenvBuildRefused) as caught:
            build.acquire(lambda: _team(indexes.registered, "wrong-password"))
        assert str(caught.value) == (
            "environment build failed: The package index 'team' refused the credential in "
            "secret 'team-index' of environment 'dev'. Set it again with `lakana secret rotate "
            "team-index --env dev --values-stdin`. If the password is right, check that its "
            "account may read the package: the index also answers 403 when the account may "
            "not read a package or file."
        )
        assert b"wrong-password" not in bytes(build.log)
        assert build.files_holding("wrong-password") == []
        venv = build.acquire(lambda: _team(indexes.registered))
    assert list(venv.glob("lib/*/site-packages/team_utils"))


def test_a_package_with_only_a_source_distribution_is_refused_with_the_message(
    tmp_path: Path,
) -> None:
    with serving() as indexes:
        build = Build(tmp_path, project_files(indexes.registered.url(), "sdonly"))
        with pytest.raises(VenvBuildRefused) as caught:
            build.acquire(lambda: _team(indexes.registered))
    assert str(caught.value).startswith(
        "environment build failed: sdonly has no wheel for this platform and Python; publish a "
        "wheel to your index, or remove the dependency."
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_with_the_lock_check_bypassed_another_host_still_gets_no_credential(
    tmp_path: Path, host: str
) -> None:
    """`acquire` runs no lock check, so this is the build with the check bypassed."""
    with serving() as indexes:
        indexes.other.server.challenge_everything = True
        build = Build(tmp_path, project_files(indexes.other.url(host)))
        with pytest.raises(VenvBuildError) as caught:
            build.acquire(lambda: _team(indexes.registered))
        assert not isinstance(caught.value, VenvBuildRefused)
        assert indexes.other.server.seen
        assert indexes.other.credentials_received == []
    assert build.files_holding(PASSWORD) == []


def test_a_second_project_gets_a_cached_private_wheel_without_asking_the_index(
    tmp_path: Path,
) -> None:
    """A documented residual (plan decision 9), pinned so that a change is noticed."""
    with serving() as indexes:
        files = project_files(indexes.registered.url())
        first = Build(tmp_path / "a", files)
        first.acquire(lambda: _team(indexes.registered))
        seen = len(indexes.registered.server.seen)
        second = Build(tmp_path / "b", {**files, "pyproject.toml": files["pyproject.toml"] + "\n"})
        second.settings = VenvSettings(
            uv=second.settings.uv,
            cache=second.settings.cache,
            uv_cache=first.settings.uv_cache,
            build_timeout=60.0,
        )
        venv = second.acquire(lambda: _team(indexes.registered, "wrong-password"))
        assert len(indexes.registered.server.seen) == seen
    assert list(venv.glob("lib/*/site-packages/team_utils"))
