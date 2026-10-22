"""The build entry of the task images: uv's flags, and what a finished environment must hold."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lakana_worker import venvs
from lakana_worker.launcher import settings as launcher_settings
from lakana_worker.launcher import task_build

_TASK_DOCKERFILE = Path(__file__).resolve().parents[3] / "Dockerfile.task"


def _environment(root: Path, *, python: bool = True, sdk: bool = True, size: int = 0) -> Path:
    """Lay out what uv would have built.

    Parameters
    ----------
    root
        The test's directory.
    python
        Whether ``bin/python`` exists.
    sdk
        Whether the `lakana` package's harness is installed.
    size
        Bytes of one extra file.

    Returns
    -------
    Path
        The environment.
    """
    env = root / "env"
    (env / "bin").mkdir(parents=True)
    if python:
        (env / "bin" / "python").write_text("")
    site = env / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    if sdk:
        (site / "lakana").mkdir()
        (site / "lakana" / "runner.py").write_text("")
    (site / "blob").write_bytes(b"x" * size)
    return env


def test_uv_runs_with_the_workers_flags_and_no_build_when_asked() -> None:
    """The same `uv sync` as the local venv cache, and `--no-build` with a credential."""
    assert task_build.uv_argv(True, "/usr/local/bin/python", Path("/src/abc")) == [
        "/usr/local/bin/uv",
        "sync",
        "--frozen",
        "--no-config",
        "--no-dev",
        "--no-default-groups",
        "--no-install-project",
        "--no-install-workspace",
        "--no-build",
        "--python",
        "/usr/local/bin/python",
        "--project",
        "/src/abc",
    ]
    assert "--no-build" not in task_build.uv_argv(False, "/usr/local/bin/python", Path("/src/abc"))


@pytest.mark.parametrize(
    ("layout", "uv", "limit", "expected"),
    [
        ({}, 0, "0", 0),
        ({}, 2, "0", 2),
        ({}, 3, "0", 1),
        ({"python": False}, 0, "0", task_build.NO_INTERPRETER),
        ({"sdk": False}, 0, "0", task_build.NO_SDK),
        ({"size": 4096}, 0, "1024", task_build.TOO_LARGE),
    ],
    ids=["built", "uv-failed", "uv-status-taken", "no-python", "no-sdk", "too-large"],
)
def test_the_exit_status_says_what_went_wrong(
    tmp_path: Path, layout: dict[str, object], uv: int, limit: str, expected: int
) -> None:
    """The status of uv passes through, except the three statuses the entry reserves.

    Parameters
    ----------
    tmp_path
        The test's directory.
    layout
        How the environment looks after uv.
    uv
        uv's exit status.
    limit
        `LAKANA_BUILD_MAX_BYTES`.
    expected
        The entry's exit status.
    """
    env = _environment(tmp_path, **layout)  # ty: ignore[invalid-argument-type]
    ran: list[list[str]] = []

    def fake_uv(argv: list[str]) -> int:
        ran.append(argv)
        return uv

    status = task_build.main(
        {"LAKANA_BUILD_NO_BUILD": "0", "LAKANA_BUILD_MAX_BYTES": limit},
        environment=env,
        project=tmp_path / "src",
        run=fake_uv,
    )

    assert status == expected
    assert ran[0][:2] == ["/usr/local/bin/uv", "sync"]


def test_the_project_defaults_to_the_launchers_mount_then_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LAKANA_BUILD_SRC` names the code's mount; without it the working directory is used.

    Parameters
    ----------
    tmp_path
        The test's directory.
    monkeypatch
        Changes the working directory.
    """
    env = _environment(tmp_path)
    ran: list[list[str]] = []

    def fake_uv(argv: list[str]) -> int:
        ran.append(argv)
        return 0

    task_build.main({"LAKANA_BUILD_SRC": "/src/abc123"}, environment=env, run=fake_uv)
    monkeypatch.chdir(tmp_path)
    task_build.main({}, environment=env, run=fake_uv)

    assert ran[0][-1] == "/src/abc123"
    assert ran[1][-1] == str(tmp_path)


def test_a_limit_that_is_not_a_number_fails_the_build(tmp_path: Path) -> None:
    """A mistyped limit must not switch the limit off.

    Parameters
    ----------
    tmp_path
        The test's directory.
    """
    env = _environment(tmp_path)

    status = task_build.main(
        {"LAKANA_BUILD_MAX_BYTES": "lots"}, environment=env, project=tmp_path, run=lambda _: 0
    )

    assert status == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_a_directory_the_size_check_cannot_read_counts_as_too_large(tmp_path: Path) -> None:
    """A build's own code could hide files from the limit with `chmod 000`.

    Parameters
    ----------
    tmp_path
        The test's directory.
    """
    env = _environment(tmp_path)
    hidden = env / "lib" / "hidden"
    hidden.mkdir()
    (hidden / "big").write_bytes(b"x" * 4096)
    hidden.chmod(0)
    try:
        status = task_build.main(
            {"LAKANA_BUILD_MAX_BYTES": "1024"}, environment=env, project=tmp_path, run=lambda _: 0
        )
    finally:
        hidden.chmod(0o755)

    assert status == task_build.TOO_LARGE


def test_the_build_entry_and_the_workers_venv_cache_share_their_uv_flags() -> None:
    """One list of flags, so the task image builds what the local cache would."""
    assert venvs.UV_SYNC_FLAGS is task_build.UV_SYNC_FLAGS


def test_the_task_images_group_is_the_launchers_task_group() -> None:
    """The launcher's `User` overrides the image's `USER`; the two must name the same ids."""
    text = _TASK_DOCKERFILE.read_text(encoding="utf-8")

    assert f"USER {launcher_settings.TASK_USER}\n" in text
    assert launcher_settings.TASK_USER == "10001:10003"
