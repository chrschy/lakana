"""`DockerEnvironments`: a built environment is used as it is; a build gets only what it needs."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fake_launcher import FakeLauncher, refusing

from lakana_core.projects.package_sources import RegisteredIndex
from lakana_worker.docker_environments import (
    PUBLIC_INDEX_HOSTS,
    DockerEnvironments,
    build_targets,
    environment_key,
    index_set,
)
from lakana_worker.index_credentials import IndexAccess
from lakana_worker.launcher.task_build import NO_INTERPRETER, NO_SDK, TOO_LARGE
from lakana_worker.runners import NO_SDK_MESSAGE
from lakana_worker.venvs import VenvBuildError, VenvBuildRefused, VenvLease

LOCK = "a" * 64
PROJECT = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER_PROJECT = uuid.UUID("00000000-0000-4000-8000-000000000002")
TEAM = RegisteredIndex("team", "https://pkgs.example.org/simple", "team-index")
OPEN = RegisteredIndex("open", "https://open.example.org/simple")
PASSWORD = "Sup3r-Secret-Pass"


def _project(tmp_path: Path) -> Path:
    """Write the two files a build reads.

    Parameters
    ----------
    tmp_path
        The test's directory.

    Returns
    -------
    Path
        The attempt's code directory.
    """
    code = tmp_path / "attempt-abc_12" / "code"
    code.mkdir(parents=True, exist_ok=True)
    (code / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    (code / "uv.lock").write_text("version = 1\n")
    return code


def _environments(launcher: FakeLauncher, **changes: object) -> DockerEnvironments:
    """Build the provider around the fake.

    Parameters
    ----------
    launcher
        The fake.
    **changes
        Fields to replace.

    Returns
    -------
    DockerEnvironments
        The provider.
    """
    values: dict[str, object] = {
        "client": launcher,
        "owner": "worker-1",
        "memory_mb": 2048,
        "cpus": 1.0,
        "pids": 256,
        "build_timeout": 600.0,
        "max_bytes": 5 * 1024**3,
    }
    values.update(changes)
    return DockerEnvironments(**values)  # ty: ignore[invalid-argument-type]


def _acquire(
    environments: DockerEnvironments,
    code: Path,
    *,
    on_log: Callable[[bytes], None] = lambda chunk: None,
    indexes: Callable[[], list[IndexAccess]] | None = None,
    used: tuple[RegisteredIndex, ...] = (),
    project_id: uuid.UUID = PROJECT,
    lock: str = LOCK,
) -> VenvLease:
    """Acquire an environment with the test's defaults.

    Parameters
    ----------
    environments
        The provider.
    code
        The code directory.
    on_log
        The log sink.
    indexes
        The index resolver.
    used
        The registered indexes the lock uses.
    project_id
        The project.
    lock
        The lock digest.

    Returns
    -------
    VenvLease
        The lease.
    """
    return environments.acquire(
        lock,
        code,
        on_log,
        indexes,
        python="3.13",
        attempt="attempt-abc_12",
        used=used,
        project_id=project_id,
    )


def _status(complete: bool) -> Callable[..., dict[str, object]]:
    """Build a status script.

    Parameters
    ----------
    complete
        The answer.

    Returns
    -------
    callable
        The script.
    """
    return lambda request, on_log: {"complete": complete}


def _resolver(*accesses: IndexAccess) -> Callable[[], list[IndexAccess]]:
    """Build an index resolver that records whether it was called.

    Parameters
    ----------
    *accesses
        What it returns.

    Returns
    -------
    callable
        The resolver, with a ``calls`` list attribute.
    """
    calls: list[int] = []

    def resolve() -> list[IndexAccess]:
        """Return the accesses and count the call.

        Returns
        -------
        list of IndexAccess
            The accesses.
        """
        calls.append(1)
        return list(accesses)

    resolve.calls = calls  # ty: ignore[unresolved-attribute]
    return resolve


def test_a_built_environment_is_used_without_a_build_or_a_secret(tmp_path: Path) -> None:
    """`status` says complete: no credential is decrypted, nothing is built."""
    launcher = FakeLauncher(scripts={"status": _status(True)})
    resolve = _resolver()

    lease = _acquire(_environments(launcher), _project(tmp_path), indexes=resolve, used=(TEAM,))

    assert lease.key == launcher.requests[0]["environment"]
    assert (lease.path, lease.python, lease.descriptor) == (
        Path("/env"),
        Path("/env/bin/python"),
        None,
    )
    assert [r["op"] for r in launcher.requests] == ["status"]
    assert resolve.calls == []  # ty: ignore[unresolved-attribute]


def test_two_projects_with_the_same_lock_never_share_an_environment(tmp_path: Path) -> None:
    """Review I4: a lock does not pin a path source's files, so the project is part of the key.

    Project A could commit B's exact `uv.lock` and `pyproject.toml` next to its own copy of a
    vendored package; with a shared key, B's tasks would then run A's code with B's token.
    """
    launcher = FakeLauncher(scripts={"status": _status(True)})
    code = _project(tmp_path)

    first = _acquire(_environments(launcher), code, project_id=PROJECT)
    second = _acquire(_environments(launcher), code, project_id=OTHER_PROJECT)

    assert first.key != second.key


def test_a_build_sends_only_credentialed_indexes_and_the_hosts(tmp_path: Path) -> None:
    """An index without a credential is a host to reach, not an entry of `UV_INDEX`."""
    launcher = FakeLauncher(
        scripts={
            "status": _status(False),
            "build": lambda r, on_log: {"complete": True, "exit": 0},
        }
    )
    team = IndexAccess(
        "team", TEAM.url, "dev", secret="team-index", username="ci", password=PASSWORD
    )
    plain = IndexAccess("open", OPEN.url, "dev")

    lease = _acquire(
        _environments(launcher),
        _project(tmp_path),
        indexes=_resolver(team, plain),
        used=(OPEN, TEAM),
    )

    build = launcher.requests[1]
    assert build["indexes"] == [{"url": TEAM.url, "username": "ci", "password": PASSWORD}]
    assert build["targets"] == [
        "pypi.org:443",
        "files.pythonhosted.org:443",
        "open.example.org:443",
        "pkgs.example.org:443",
    ]
    assert (build["no_build"], build["attempt"], build["operator_index"]) == (
        True,
        "attempt-abc_12",
        None,
    )
    assert build["timeout_seconds"] == 600
    assert (build["environment"], build["python"], build["owner"]) == (
        lease.key,
        "3.13",
        "worker-1",
    )
    assert len(build["build"]) == 32 and int(build["build"], 16) >= 0
    assert (build["memory_mb"], build["cpus"], build["pids"], build["max_bytes"]) == (
        2048,
        1.0,
        256,
        5 * 1024**3,
    )


def test_an_index_password_split_across_log_lines_is_redacted(tmp_path: Path) -> None:
    """Review focus: uv may print a URL with the credential in two pieces."""
    team = IndexAccess(
        "team", TEAM.url, "dev", secret="team-index", username="ci", password=PASSWORD
    )

    def build(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Print the credential in two chunks.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            A complete build.
        """
        on_log(b"GET https://ci:" + PASSWORD[:6].encode())
        on_log(PASSWORD[6:].encode() + b"@pkgs.example.org/simple\n")
        return {"complete": True, "exit": 0}

    launcher = FakeLauncher(scripts={"status": _status(False), "build": build})
    logs: list[bytes] = []

    _acquire(
        _environments(launcher),
        _project(tmp_path),
        on_log=logs.append,
        indexes=_resolver(team),
        used=(TEAM,),
    )

    assert PASSWORD.encode() not in b"".join(logs)
    assert b"pkgs.example.org/simple" in b"".join(logs)


def test_the_operator_index_password_is_redacted_and_sent(tmp_path: Path) -> None:
    """`LAKANA_RUN__INDEX_URL` reaches the launcher whole; its password never reaches a log."""
    operator = "https://op:Op3rat0r-Pass@mirror.example.org/simple"

    def build(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Print the operator's password.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            A complete build.
        """
        on_log(b"password Op3rat0r-Pass\n")
        return {"complete": True}

    launcher = FakeLauncher(scripts={"status": _status(False), "build": build})
    logs: list[bytes] = []
    environments = _environments(launcher, operator_index=operator)
    assert "Op3rat0r-Pass" not in repr(environments)

    _acquire(environments, _project(tmp_path), on_log=logs.append)

    assert launcher.requests[1]["operator_index"] == operator
    assert launcher.requests[1]["targets"] == ["mirror.example.org:443"]
    assert b"Op3rat0r-Pass" not in b"".join(logs)


@pytest.mark.parametrize(
    ("result", "error", "message"),
    [
        ({"exit": NO_SDK}, VenvBuildRefused, NO_SDK_MESSAGE),
        ({"exit": NO_INTERPRETER}, VenvBuildError, "uv did not create an interpreter"),
        ({"exit": TOO_LARGE}, VenvBuildError, "the venv exceeded 5368709120 bytes"),
        ({"exit": 137, "timed_out": True}, VenvBuildError, "timed out after 600s"),
        ({"exit": 137, "oom_killed": True}, VenvBuildError, "LAKANA_RUN__TASK_MEMORY"),
        ({"exit": 2}, VenvBuildError, "uv exited with status 2"),
    ],
)
def test_build_failures_map_to_the_venv_errors(
    tmp_path: Path, result: dict[str, object], error: type[Exception], message: str
) -> None:
    """The same errors, and so the same task failures, as the local venv cache.

    Parameters
    ----------
    tmp_path
        The test's directory.
    result
        The launcher's build result (incomplete).
    error
        The exception expected.
    message
        Part of its text.
    """
    launcher = FakeLauncher(
        scripts={
            "status": _status(False),
            "build": lambda r, on_log: {"complete": False, **result},
        }
    )

    with pytest.raises(error, match="environment build failed") as caught:
        _acquire(_environments(launcher), _project(tmp_path))
    assert message in str(caught.value)
    if error is VenvBuildError:
        assert not isinstance(caught.value, VenvBuildRefused)


def test_an_index_refusing_the_credential_is_permanent(tmp_path: Path) -> None:
    """A 401 from a registered index reads as the local cache reads it."""
    team = IndexAccess(
        "team", TEAM.url, "dev", secret="team-index", username="ci", password=PASSWORD
    )

    def build(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Fail as uv does when the index refuses the credential.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            A failed build.
        """
        on_log(
            b"error: Failed to fetch: `https://pkgs.example.org/simple/demo/`\n"
            b"  Caused by: HTTP status client error (401 Unauthorized) for url "
            b"(https://pkgs.example.org/simple/demo/)\n"
        )
        return {"complete": False, "exit": 1}

    launcher = FakeLauncher(scripts={"status": _status(False), "build": build})

    with pytest.raises(VenvBuildRefused, match="environment build failed"):
        _acquire(_environments(launcher), _project(tmp_path), indexes=_resolver(team), used=(TEAM,))


def test_a_missing_wheel_with_a_credential_is_permanent(tmp_path: Path) -> None:
    """`--no-build` with a credential in play: an sdist-only package cannot be installed."""
    team = IndexAccess(
        "team", TEAM.url, "dev", secret="team-index", username="ci", password=PASSWORD
    )

    def build(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Fail as uv does for a package without a wheel under `--no-build`.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            A failed build.
        """
        on_log(
            b"error: Distribution `demo==1.0 @ registry+x` can't be installed because it is "
            b"marked as `--no-build` but has no binary distribution\n"
        )
        return {"complete": False, "exit": 1}

    launcher = FakeLauncher(scripts={"status": _status(False), "build": build})

    with pytest.raises(VenvBuildRefused, match="demo"):
        _acquire(_environments(launcher), _project(tmp_path), indexes=_resolver(team), used=(TEAM,))


def test_a_refused_build_is_permanent_and_an_unreachable_launcher_transient(
    tmp_path: Path,
) -> None:
    """A refusal will not change by retrying; a launcher that is down may come back."""
    refused = FakeLauncher(scripts={"status": _status(False), "build": refusing("hosts: ...")})
    down = FakeLauncher()
    code = _project(tmp_path)

    with pytest.raises(VenvBuildRefused, match="the launcher refused the build: hosts"):
        _acquire(_environments(refused), code)
    with pytest.raises(VenvBuildError, match="runner unavailable") as caught:
        _acquire(_environments(down), code)
    assert not isinstance(caught.value, VenvBuildRefused)


def test_a_bad_lock_digest_or_a_missing_pyproject_fails_before_the_launcher(
    tmp_path: Path,
) -> None:
    """Nothing is asked of the launcher for a task the worker cannot key."""
    launcher = FakeLauncher()

    with pytest.raises(VenvBuildError, match="no valid lock digest"):
        _acquire(_environments(launcher), _project(tmp_path), lock="A" * 64)
    with pytest.raises(VenvBuildError, match=r"no readable pyproject\.toml"):
        _acquire(_environments(launcher), tmp_path / "missing")
    assert launcher.requests == []


def test_the_key_covers_project_lock_pyproject_python_and_index_set() -> None:
    """Any of the five changes the environment; a credential does not."""
    base = environment_key(PROJECT, LOCK, "b" * 64, "3.13", index_set((TEAM,), None))

    assert base != environment_key(OTHER_PROJECT, LOCK, "b" * 64, "3.13", index_set((TEAM,), None))
    assert base != environment_key(PROJECT, LOCK, "b" * 64, "3.12", index_set((TEAM,), None))
    assert base != environment_key(PROJECT, LOCK, "c" * 64, "3.13", index_set((TEAM,), None))
    assert base != environment_key(PROJECT, LOCK, "b" * 64, "3.13", index_set((TEAM, OPEN), None))
    assert base != environment_key(
        PROJECT, LOCK, "b" * 64, "3.13", index_set((TEAM,), "https://m.example.org/simple")
    )
    assert index_set((), "https://user:pw@m.example.org/simple") == index_set(
        (), "https://m.example.org/simple"
    )
    material = f"project:{PROJECT}:{LOCK}:{'b' * 64}:py3.13:{index_set((TEAM,), None)}"
    assert base == hashlib.sha256(material.encode()).hexdigest()


def test_the_operator_index_replaces_the_public_hosts() -> None:
    """With `LAKANA_RUN__INDEX_URL` the build reaches that host, not PyPI's."""
    assert list(PUBLIC_INDEX_HOSTS) == ["pypi.org", "files.pythonhosted.org"]
    assert build_targets((), None) == ["pypi.org:443", "files.pythonhosted.org:443"]
    assert build_targets((TEAM,), "https://op:pw@Mirror.Example.org/simple") == [
        "mirror.example.org:443",
        "pkgs.example.org:443",
    ]


def test_every_index_is_sent_with_its_port() -> None:
    """#301: an index on another port reaches the launcher with it, credential or not."""
    odd = RegisteredIndex("odd", "https://Odd.Example.org:8443/simple")
    again = RegisteredIndex("again", "https://odd.example.org/other")

    assert build_targets((odd, again, TEAM), "https://mirror.example.org:9443/simple") == [
        "mirror.example.org:9443",
        "odd.example.org:8443",
        "odd.example.org:443",
        "pkgs.example.org:443",
    ]
