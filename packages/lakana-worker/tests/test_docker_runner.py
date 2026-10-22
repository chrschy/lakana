"""`DockerRunner`: the task through the launcher, and how each end maps to today's outcomes."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from fake_launcher import FakeLauncher, refusing

from lakana_core.runs.failures import FailureCode
from lakana_worker.launcher_client import LauncherUnavailable, LogSinkFailed
from lakana_worker.runners import (
    DockerRunner,
    PythonTask,
    RunOutcome,
    StopReason,
    StopSignal,
    memory_message,
)

TOKEN = "lakana_task_key01_" + "s" * 40


def _task(tmp_path: Path, **fields: object) -> PythonTask:
    """Build a task whose attempt directory is private.

    Parameters
    ----------
    tmp_path
        The test's directory.
    **fields
        Fields to replace.

    Returns
    -------
    PythonTask
        The task.
    """
    attempt = tmp_path / "attempt-abc_12"
    attempt.mkdir(mode=0o700, parents=True, exist_ok=True)
    attempt.chmod(0o700)
    values: dict[str, object] = {
        "code_dir": attempt / "code",
        "asset_path": "assets/gold/scores.py",
        "inputs": {},
        "table": "gold.scores",
        "environment": "dev",
        "server_url": "http://server:8000",
        "token": TOKEN,
        "timeout_seconds": 30.0,
        "venv": Path("/env"),
        "expected_snapshot_id": "none",
        "attempt_dir": attempt,
        "environment_key": "e" * 64,
        "python": "3.13",
    }
    values.update(fields)
    return PythonTask(**values)  # ty: ignore[invalid-argument-type]


def _runner(launcher: FakeLauncher, **options: object) -> DockerRunner:
    """Build the runner around the fake.

    Parameters
    ----------
    launcher
        The fake.
    **options
        Keyword options to replace.

    Returns
    -------
    DockerRunner
        The runner.
    """
    values: dict[str, object] = {
        "owner": "worker-1",
        "memory_mb": 2048,
        "cpus": 1.0,
        "pids": 256,
        "kill_grace": 3.0,
        "poll_seconds": 0.01,
    }
    values.update(options)
    return DockerRunner(launcher, **values)  # ty: ignore[invalid-argument-type]


def _writes(
    record: Mapping[str, object] | None,
    result: Mapping[str, object],
    logs: tuple[bytes, ...] = (),
) -> Callable[..., dict[str, object]]:
    """Build a run script that logs, writes the harness's record and ends.

    Parameters
    ----------
    record
        The result record to write into the work directory, or `None`.
    result
        The launcher's result.
    logs
        Output chunks.

    Returns
    -------
    callable
        The script.
    """

    def script(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Log, write the record, return the result.

        Parameters
        ----------
        request
            The request, with the test's ``_attempt_dir``.
        on_log
            The log sink.

        Returns
        -------
        dict
            The launcher's result.
        """
        for chunk in logs:
            on_log(chunk)
        if record is not None:
            work = Path(request["_attempt_dir"]) / "task"
            (work / "result.json").write_text(json.dumps(record))
        return dict(result)

    return script


def _run(
    tmp_path: Path,
    launcher: FakeLauncher,
    runner: dict[str, object] | None = None,
    /,
    **task: object,
) -> tuple[RunOutcome, list[bytes], StopSignal]:
    """Run one task, with the attempt directory passed to the script.

    Parameters
    ----------
    tmp_path
        The test's directory.
    launcher
        The fake.
    runner
        Options of the runner to replace.
    **task
        Fields of the task.

    Returns
    -------
    tuple
        The outcome, the log chunks and the stop signal.
    """
    job = _task(tmp_path, **task)
    original = launcher.call

    def call(
        request: dict[str, Any], on_log: Callable[[bytes], None] | None = None
    ) -> dict[str, object]:
        """Pass the attempt directory to the script.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            The script's result.
        """
        return original({**request, "_attempt_dir": str(job.attempt_dir)}, on_log)

    launcher.call = call  # ty: ignore[invalid-assignment]
    logs: list[bytes] = []
    stop = StopSignal()
    return _runner(launcher, **(runner or {})).run(job, logs.append, stop), logs, stop


_DONE = {"exit": 0, "oom_killed": False, "timed_out": False, "stopped": False}


def test_a_run_reads_the_record_from_the_work_directory(tmp_path: Path) -> None:
    """The harness writes `/work/result.json`, which is `<attempt>/task/result.json` here."""
    launcher = FakeLauncher(scripts={"run": _writes({"status": "succeeded", "rows": 3}, _DONE)})

    outcome, _, _ = _run(tmp_path, launcher)

    assert (outcome.succeeded, outcome.rows, outcome.failure_code) == (True, 3, None)


def test_a_stale_record_from_an_earlier_run_is_removed_first(tmp_path: Path) -> None:
    """A record left in the work directory must not count for this run."""
    work = tmp_path / "attempt-abc_12" / "task"
    work.mkdir(parents=True, mode=0o700)
    (tmp_path / "attempt-abc_12").chmod(0o700)
    (work / "result.json").write_text(json.dumps({"status": "succeeded", "rows": 3}))

    outcome, _, _ = _run(tmp_path, FakeLauncher(scripts={"run": _writes(None, _DONE)}))

    assert outcome.succeeded is False
    assert outcome.error_message == "the task process exited with status 0 without a result"


def test_the_request_carries_container_paths_and_no_parent_pid(tmp_path: Path) -> None:
    """The description is the local one with the container's paths; the token is on stdin only."""
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE)})

    _run(tmp_path, launcher)

    (request,) = launcher.requests
    description = json.loads(request["stdin"])
    assert (description["result_path"], description["work_dir"], description["parent_pid"]) == (
        "/work/result.json",
        "/work",
        None,
    )
    assert description["token"] == TOKEN
    assert TOKEN not in json.dumps({k: v for k, v in request.items() if k != "stdin"})
    assert {
        k: request[k]
        for k in (
            "op",
            "owner",
            "python",
            "environment",
            "attempt",
            "memory_mb",
            "cpus",
            "pids",
            "timeout_seconds",
            "kill_grace_seconds",
            "roots",
        )
    } == {
        "op": "run",
        "owner": "worker-1",
        "python": "3.13",
        "environment": "e" * 64,
        "attempt": "attempt-abc_12",
        "memory_mb": 2048,
        "cpus": 1.0,
        "pids": 256,
        "timeout_seconds": 30,
        "kill_grace_seconds": 3,
        "roots": [],
    }
    assert set(request) - {"_attempt_dir"} == {
        "op", "task", "owner", "python", "environment", "attempt", "memory_mb", "cpus",
        "pids", "timeout_seconds", "kill_grace_seconds", "stdin", "roots",
    }  # fmt: skip
    work = tmp_path / "attempt-abc_12" / "task"
    assert all((work / d).is_dir() for d in ("home", "tmp", "tmp/polars-spill"))
    assert oct(work.stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize(
    ("timeout", "grace", "sent_timeout", "sent_grace"),
    [
        (0.2, -1.0, 1, 0),
        (30.5, 2.5, 31, 3),
        (10.0, 1000.0, 10, 300),
        (10.0, float("inf"), 10, 300),
        (10.0, float("nan"), 10, 0),
    ],
)
def test_the_limits_are_whole_seconds_within_the_launcher_rules(
    tmp_path: Path, timeout: float, grace: float, sent_timeout: int, sent_grace: int
) -> None:
    """The launcher takes integers: a timeout of at least 1, a grace from 0 to 300.

    Parameters
    ----------
    tmp_path
        The test's directory.
    timeout
        The asset's timeout.
    grace
        `LAKANA_RUN__KILL_GRACE`.
    sent_timeout
        What the launcher gets.
    sent_grace
        What the launcher gets.
    """
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE)})

    _run(tmp_path, launcher, {"kill_grace": grace}, timeout_seconds=timeout)

    request = launcher.requests[0]
    assert (request["timeout_seconds"], request["kill_grace_seconds"]) == (sent_timeout, sent_grace)


def test_the_ingestion_roots_are_sent(tmp_path: Path) -> None:
    """The launcher mounts only roots on its own allow-list; the worker names them."""
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE)})

    _run(tmp_path, launcher, {"roots": ("landing",)})

    assert launcher.requests[0]["roots"] == ["landing"]


def test_an_out_of_memory_kill_is_the_memory_stop_reason(tmp_path: Path) -> None:
    """Docker's `OOMKilled` replaces the worker's watchdog for task memory."""
    result = {"exit": 137, "oom_killed": True, "timed_out": False, "stopped": False}
    launcher = FakeLauncher(scripts={"run": _writes(None, result)})

    outcome, _, stop = _run(tmp_path, launcher)

    assert stop.reason is StopReason.MEMORY
    assert (outcome.succeeded, outcome.killed, outcome.retryable) == (False, True, True)
    assert outcome.error_message == memory_message(2048)
    assert outcome.failure_code is FailureCode.MEMORY
    assert "LAKANA_RUN__TASK_MEMORY" in memory_message(2048)


def test_a_timeout_is_reported_as_one(tmp_path: Path) -> None:
    """The launcher's timer stopped the container."""
    result = {"exit": 137, "oom_killed": False, "timed_out": True, "stopped": False}

    outcome, _, _ = _run(tmp_path, FakeLauncher(scripts={"run": _writes(None, result)}))

    assert (outcome.timed_out, outcome.error_message) == (True, "timed out after 30s")
    assert outcome.failure_code is FailureCode.TIMEOUT


def test_a_success_recorded_before_a_timeout_still_succeeds(tmp_path: Path) -> None:
    """The record is written after the upload returned; a hang afterwards changes nothing."""
    result = {"exit": 137, "oom_killed": False, "timed_out": True, "stopped": False}
    record = {"status": "succeeded", "rows": 1}

    outcome, _, _ = _run(tmp_path, FakeLauncher(scripts={"run": _writes(record, result)}))

    assert outcome.succeeded is True


def test_a_stop_asks_the_launcher_to_stop_the_container(tmp_path: Path) -> None:
    """A cancel, a lost lease or the drain reaches the container through `stop`."""
    launcher = FakeLauncher()

    def blocks(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Run until stopped.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            A stopped container.
        """
        launcher.released.wait(10)
        return {"exit": 137, "oom_killed": False, "timed_out": False, "stopped": True}

    launcher.scripts["run"] = blocks
    job = _task(tmp_path)
    stop = StopSignal()
    threading.Timer(0.1, stop.set, (StopReason.CANCELLED,)).start()

    outcome = _runner(launcher).run(job, lambda chunk: None, stop)

    assert launcher.stopped == [launcher.requests[0]["task"]]
    assert outcome.error_message == "stopped: cancelled"
    assert outcome.failure_code is FailureCode.CANCELLED


def test_a_stop_the_launcher_cannot_take_is_not_raised(tmp_path: Path) -> None:
    """The launcher's own deadline still ends the container; the run waits for its answer."""
    launcher = FakeLauncher()
    stop = StopSignal()
    stop.set(StopReason.DRAIN)

    def down(ident: str) -> bool:
        """Fail like an unreachable launcher.

        Parameters
        ----------
        ident
            The id.

        Raises
        ------
        LauncherUnavailable
            Always.
        """
        raise LauncherUnavailable("runner unavailable: gone")

    def slow(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """End stopped after a moment.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            A stopped container.
        """
        threading.Event().wait(0.1)
        return {"exit": 137, "stopped": True}

    launcher.stop = down  # ty: ignore[invalid-assignment]
    launcher.scripts["run"] = slow

    outcome = _runner(launcher).run(_task(tmp_path), lambda chunk: None, stop)

    assert outcome.error_message == "stopped: worker draining"
    assert outcome.failure_code is FailureCode.WORKER_LOST


def test_a_cancel_wins_over_an_out_of_memory_kill_after_it(tmp_path: Path) -> None:
    """The first reason wins, as with the watchdog."""
    launcher = FakeLauncher(
        scripts={"run": _writes(None, {"exit": 137, "oom_killed": True, "stopped": True})}
    )
    job = _task(tmp_path)
    stop = StopSignal()
    stop.set(StopReason.CANCELLED)

    _runner(launcher).run(job, lambda chunk: None, stop)

    assert stop.reason is StopReason.CANCELLED


def test_an_unreachable_launcher_is_runner_unavailable_and_transient(tmp_path: Path) -> None:
    """The message names the service and the setting (`unavailable_message`)."""
    launcher = FakeLauncher()

    outcome, _, _ = _run(tmp_path, launcher)

    assert outcome.succeeded is False and outcome.retryable is True
    assert outcome.error_message == "runner unavailable: no script for run"
    assert outcome.failure_code is FailureCode.NO_RUNTIME


def test_a_refused_run_is_permanent_and_names_the_settings(tmp_path: Path) -> None:
    """A refusal is a configuration mismatch between worker and launcher; a retry cannot help."""
    launcher = FakeLauncher(
        scripts={"run": refusing("python must be one of 3.14 (LAKANA_LAUNCHER__IMAGES)")}
    )

    outcome, _, _ = _run(tmp_path, launcher)

    assert outcome.retryable is False
    assert outcome.failure_code is FailureCode.NO_RUNTIME
    assert outcome.error_message == (
        "the launcher refused the task: python must be one of 3.14 (LAKANA_LAUNCHER__IMAGES). "
        "An operator checks LAKANA_LAUNCHER__IMAGES and the LAKANA_RUN__TASK_* settings against "
        "the launcher's ceilings, or the project's python: in lakana.yaml"
    )


def test_a_task_token_split_across_log_lines_is_redacted(tmp_path: Path) -> None:
    """Review focus: Docker frames and base64 lines cut the token anywhere."""
    middle = len(TOKEN) // 2
    chunks = (b"token=" + TOKEN[:middle].encode(), TOKEN[middle:].encode() + b"\n")
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE, chunks)})

    _, logs, _ = _run(tmp_path, launcher)

    joined = b"".join(logs)
    assert TOKEN.encode() not in joined and b"s" * 40 not in joined
    assert joined.startswith(b"token=")


def test_a_failing_log_sink_stops_the_container_and_is_permanent(tmp_path: Path) -> None:
    """The client reports the sink's failure; the runner stops the container."""
    launcher = FakeLauncher()

    def sink_fails(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Fail as the client does when the sink raised.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Raises
        ------
        LogSinkFailed
            Always.
        """
        raise LogSinkFailed("the log sink failed")

    launcher.scripts["run"] = sink_fails

    outcome, _, _ = _run(tmp_path, launcher)

    assert (outcome.error_message, outcome.retryable) == ("the log sink failed", False)
    assert outcome.failure_code is FailureCode.INTERNAL
    assert launcher.stopped == [launcher.requests[0]["task"]]


def test_a_sink_failing_on_the_last_flush_fails_an_unsuccessful_run(tmp_path: Path) -> None:
    """The redactor flushes what it kept when the run ends; that flush may fail too."""
    launcher = FakeLauncher(scripts={"run": _writes(None, {"exit": 1}, (b"no newline",))})

    def broken(chunk: bytes) -> None:
        """Fail on every write.

        Parameters
        ----------
        chunk
            The output.

        Raises
        ------
        OSError
            Always.
        """
        raise OSError("disk full")

    outcome = _runner(launcher).run(_task(tmp_path), broken, StopSignal())

    assert (outcome.error_message, outcome.retryable) == ("the log sink failed", False)
    assert outcome.failure_code is FailureCode.INTERNAL


@pytest.mark.parametrize(
    "fields",
    [
        {"environment_key": None},
        {"python": None},
        {"timeout_seconds": float("inf")},
        {"attempt_dir": Path("relative/attempt-abc_12")},
    ],
    ids=["no-key", "no-python", "infinite-timeout", "relative-dir"],
)
def test_an_invalid_task_is_a_worker_defect(tmp_path: Path, fields: dict[str, object]) -> None:
    """Nothing is sent: the worker built the task wrong.

    Parameters
    ----------
    tmp_path
        The test's directory.
    fields
        The wrong fields.
    """
    launcher = FakeLauncher()

    outcome, _, _ = _run(tmp_path, launcher, **fields)

    assert launcher.requests == []
    assert outcome.retryable is False and "worker defect" in (outcome.error_message or "")
    assert outcome.failure_code is FailureCode.INTERNAL


def test_an_attempt_directory_others_can_read_is_refused(tmp_path: Path) -> None:
    """The work directory holds the result record; nobody else may plant one."""
    launcher = FakeLauncher()
    job = _task(tmp_path)
    job.attempt_dir.chmod(0o755)

    outcome = _runner(launcher).run(job, lambda chunk: None, StopSignal())

    assert launcher.requests == []
    assert "not private" in (outcome.error_message or "")
    assert outcome.failure_code is FailureCode.INTERNAL


def test_a_work_directory_that_is_a_link_is_refused(tmp_path: Path) -> None:
    """The container writes into `<attempt>/task`; a link there would point it elsewhere."""
    launcher = FakeLauncher()
    job = _task(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (job.attempt_dir / "task").symlink_to(elsewhere)

    outcome = _runner(launcher).run(job, lambda chunk: None, StopSignal())

    assert launcher.requests == []
    assert "work directory is not private" in (outcome.error_message or "")
    assert outcome.failure_code is FailureCode.INTERNAL
    assert list(elsewhere.iterdir()) == []


def test_a_work_directory_that_cannot_be_made_fails_before_the_launcher(tmp_path: Path) -> None:
    """A file where the work directory belongs is reported, not raised."""
    launcher = FakeLauncher()
    job = _task(tmp_path)
    (job.attempt_dir / "task").write_text("in the way")
    second = _task(tmp_path / "second")
    (second.attempt_dir / "task").mkdir(mode=0o700)
    (second.attempt_dir / "task" / "home").write_text("in the way")

    for attempt in (job, second):
        outcome = _runner(launcher).run(attempt, lambda chunk: None, StopSignal())

        assert (outcome.error_message or "").startswith("could not prepare the task directory")
        assert outcome.failure_code is FailureCode.INTERNAL
    assert launcher.requests == []


def test_an_exit_by_signal_reads_as_the_signal(tmp_path: Path) -> None:
    """The container's init reports 128 + the signal; the message names the signal."""
    result = {"exit": 137, "oom_killed": False, "timed_out": False, "stopped": False}

    outcome, _, _ = _run(tmp_path, FakeLauncher(scripts={"run": _writes(None, result)}))

    assert outcome.error_message == "the task process was killed by signal 9"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "ended without an exit status from Docker"),
        (True, "ended without an exit status from Docker"),
        (2, "the harness refuses a task description"),
        (160, "exited with status 160"),
        (128, "exited with status 128"),
    ],
)
def test_an_exit_status_docker_gave_oddly_is_read_safely(
    tmp_path: Path, value: object, message: str
) -> None:
    """No status, a boolean, or a status outside the signal range is a plain status.

    Parameters
    ----------
    tmp_path
        The test's directory.
    value
        Docker's status.
    message
        Part of the failure's text.
    """
    launcher = FakeLauncher(scripts={"run": _writes(None, {"exit": value})})

    outcome, _, _ = _run(tmp_path, launcher)

    assert message in (outcome.error_message or "")
    assert outcome.succeeded is False and outcome.failure_code is not None


def test_a_stopped_container_without_a_status_reads_as_stopped(tmp_path: Path) -> None:
    """A stop explains the end; Docker's missing status does not matter then."""
    launcher = FakeLauncher(scripts={"run": _writes(None, {"stopped": True})})

    outcome, _, _ = _run(tmp_path, launcher)

    assert outcome.error_message == "stopped: stopped"


def test_an_unavailable_launcher_raised_by_the_client_is_kept_as_its_text(tmp_path: Path) -> None:
    """`LauncherUnavailable`'s text is already the task's message."""
    launcher = FakeLauncher()

    def down(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Fail like a missing socket.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Raises
        ------
        LauncherUnavailable
            Always.
        """
        raise LauncherUnavailable(
            "runner unavailable: the launcher's socket cannot be reached (No such file or "
            "directory). Check ..."
        )

    launcher.scripts["run"] = down

    outcome, _, _ = _run(tmp_path, launcher)

    assert (outcome.error_message or "").startswith(
        "runner unavailable: the launcher's socket cannot be reached"
    )


def test_an_unexpected_error_of_the_client_is_raised(tmp_path: Path) -> None:
    """A bug is not turned into a task failure that hides it."""
    launcher = FakeLauncher()

    def bug(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Fail like a bug.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Raises
        ------
        KeyError
            Always.
        """
        raise KeyError("bug")

    launcher.scripts["run"] = bug

    with pytest.raises(KeyError):
        _run(tmp_path, launcher)
