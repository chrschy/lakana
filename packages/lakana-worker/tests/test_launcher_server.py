"""The launcher's Unix socket: the token, one request per connection, and a caller that leaves."""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from launcher_fakes import FakeEngine, settings

from lakana_worker.launcher import server as server_module
from lakana_worker.launcher.engine import EngineError
from lakana_worker.launcher.requests import StopRequest, environment_volume
from lakana_worker.launcher.server import TOKEN_FILE, LauncherServer, serve, write_token
from lakana_worker.launcher.service import Launcher

RUN = {
    "op": "run",
    "task": "t1",
    "owner": "w1",
    "python": "3.13",
    "environment": "a" * 64,
    "attempt": "attempt-x1",
    "memory_mb": 256,
    "cpus": 1,
    "pids": 64,
    "timeout_seconds": 30,
    "kill_grace_seconds": 1,
    "stdin": "{}",
    "roots": [],
}


@dataclass
class Served:
    """A running launcher server and what a test needs to reach it.

    Parameters
    ----------
    engine
        The fake Docker engine.
    token
        The token every request carries.
    server
        The socket server.
    launcher
        The launcher behind it.
    """

    engine: FakeEngine
    token: str
    server: LauncherServer
    launcher: Launcher


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    """Serve a started launcher on a short relative socket path in the test's directory.

    Parameters
    ----------
    tmp_path
        The test's directory, which becomes the working directory (a Unix socket path is
        limited to 108 bytes).
    monkeypatch
        Changes the working directory.

    Yields
    ------
    Served
        The running server.
    """
    monkeypatch.chdir(tmp_path)
    engine = FakeEngine(hold=True)
    launcher = Launcher(settings(tmp_path), engine)
    launcher.start()
    volume = environment_volume(launcher.settings, "3.13", "a" * 64)
    launcher._mark_complete(volume)
    engine.volumes.add(volume)
    token = write_token(Path("."))
    server = LauncherServer("l.sock", launcher=launcher, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Served(engine, token, server, launcher)
    server.shutdown()
    server.server_close()


def _call(request: dict[str, object]) -> list[dict[str, object]]:
    """Send one request and read every answer line.

    Parameters
    ----------
    request
        The request.

    Returns
    -------
    list of dict
        The decoded lines.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
        client.sendall(json.dumps(request).encode() + b"\n")
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    return [json.loads(line) for line in data.splitlines()]


def _wait_for(condition: Callable[[], object], seconds: float = 5.0) -> None:
    """Wait for a condition, failing after a bound.

    Parameters
    ----------
    condition
        A callable returning something truthy when done.
    seconds
        The bound.
    """
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the condition was never met"
        time.sleep(0.01)


def test_a_request_without_the_token_is_refused_and_the_log_has_no_value(
    served: Served, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal names the rule and the fix; the log line holds no field of the request."""
    caplog.set_level(logging.WARNING)

    answer = _call({**RUN, "token": "not-it", "stdin": "SENTINEL"})

    assert len(answer) == 1 and "LAKANA_RUN__LAUNCHER_SOCKET" in str(answer[0]["refused"])
    assert "SENTINEL" not in caplog.text and "not-it" not in caplog.text


def test_a_run_streams_its_logs_then_its_result(served: Served) -> None:
    """Log lines come first, then exactly one result line."""
    served.engine.hold = False

    answer = _call({**RUN, "token": served.token})

    assert base64.b64decode(str(answer[0]["log"])) == b"hello\n"
    assert answer[-1] == {
        "result": {"exit": 0, "oom_killed": False, "timed_out": False, "stopped": False}
    }
    assert served.launcher._abandoned == set()


def test_stop_status_and_cleanup_answer_one_result_line(served: Served) -> None:
    """The short operations need no stream."""
    token = served.token

    assert _call({"op": "stop", "token": token, "id": "t9"}) == [{"result": {"stopped": False}}]
    assert _call({"op": "status", "token": token, "python": "3.13", "environment": "a" * 64}) == [
        {"result": {"complete": True}}
    ]
    assert _call({"op": "cleanup", "token": token, "owner": "w1"}) == [{"result": {"removed": 0}}]


def test_the_token_file_is_readable_by_the_group_only(served: Served) -> None:
    """The worker shares the launcher's group through the volume; nobody else reads it."""
    mode = stat.S_IMODE(Path(TOKEN_FILE).stat().st_mode)

    assert mode == 0o640


def test_the_token_file_mode_does_not_depend_on_the_umask(tmp_path: Path) -> None:
    """The mode is set on the open file, so no path can be swapped for a link in between."""
    old = os.umask(0o277)
    try:
        token = write_token(tmp_path)
    finally:
        os.umask(old)

    written = tmp_path / TOKEN_FILE
    assert stat.S_IMODE(written.stat().st_mode) == 0o640
    assert written.read_text(encoding="ascii") == token and len(token) == 64
    assert [p.name for p in tmp_path.iterdir()] == [TOKEN_FILE]


def test_a_token_is_new_each_time_and_replaces_the_old_file(tmp_path: Path) -> None:
    """A restart invalidates what a worker read before; a stale temporary file is cleared."""
    stale = tmp_path / f".{TOKEN_FILE}.{os.getpid()}"
    stale.write_text("stale", encoding="ascii")

    first = write_token(tmp_path)
    second = write_token(tmp_path)

    assert first != second and (tmp_path / TOKEN_FILE).read_text(encoding="ascii") == second


def test_an_engine_failure_answers_error_with_its_text(served: Served) -> None:
    """The worker turns an `error` line into "runner unavailable"."""
    served.engine.services = {}

    answer = _call({**RUN, "token": served.token})

    assert answer == [
        {
            "error": "the server service of this stack is not running; start it "
            "(docker compose up -d server)"
        }
    ]


def test_an_error_that_is_not_the_callers_is_answered_and_logged(
    served: Served, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk (an OSError) is not a caller that left: the worker is told."""

    def broken(request: object, emit: object) -> dict[str, object]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(served.launcher, "handle", broken)

    answer = _call({**RUN, "token": served.token})

    assert answer == [{"error": "launcher internal error; see docker compose logs launcher"}]
    assert "No space left on device" in caplog.text


def test_a_failure_of_the_launcher_itself_is_answered_as_an_internal_error(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any exception is an answer, never a dropped connection."""

    def broken(request: object, emit: object) -> dict[str, object]:
        raise KeyError("Id")

    monkeypatch.setattr(served.launcher, "handle", broken)

    assert _call({"op": "cleanup", "token": served.token, "owner": "w1"}) == [
        {"error": "launcher internal error; see docker compose logs launcher"}
    ]


def test_a_failure_to_answer_does_not_stop_the_server(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that leaves before its error line arrives costs nothing but a log line."""

    def broken(request: object, emit: object) -> dict[str, object]:
        raise EngineError("down")

    monkeypatch.setattr(served.launcher, "handle", broken)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
        client.sendall(
            json.dumps({"op": "cleanup", "token": served.token, "owner": "w1"}).encode() + b"\n"
        )
    monkeypatch.delattr(served.launcher, "handle")

    assert _call({"op": "stop", "token": served.token, "id": "t9"}) == [
        {"result": {"stopped": False}}
    ]


def test_a_client_that_disconnects_mid_run_gets_its_container_removed(served: Served) -> None:
    """Review focus: the watcher sees the closed connection and the launcher kills the task."""
    engine = served.engine
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect("l.sock")
    client.sendall(json.dumps({**RUN, "token": served.token}).encode() + b"\n")
    _wait_for(lambda: sum(path.endswith("/start") for _, path, _ in engine.calls) >= 2)

    client.close()

    _wait_for(lambda: not engine.containers and not engine.networks)


def test_a_client_that_leaves_while_logs_flow_does_not_leave_the_task_running(
    served: Served,
) -> None:
    """Writing to a closed connection ends the run through the caller-gone path."""
    served.engine.hold = False
    served.engine.output = [b"x" * 60000] * 200
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect("l.sock")
    client.sendall(json.dumps({**RUN, "token": served.token}).encode() + b"\n")
    client.recv(10)

    client.close()

    _wait_for(lambda: not served.engine.containers and not served.engine.networks)
    _wait_for(lambda: not served.launcher._live)


def test_a_silent_connection_is_closed_and_frees_its_slot(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a bound 128 idle connections would block every run; the watcher is not bounded."""
    monkeypatch.setattr(server_module, "_READ_TIMEOUT", 0.2)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
        data = client.recv(4096)

    assert json.loads(data) == {
        "refused": "no request line arrived in time; send the request at once"
    }
    assert served.server.slots._value == server_module._MAX_CONNECTIONS  # type: ignore[attr-defined]


def test_too_many_connections_are_refused_with_the_next_step(served: Served) -> None:
    """The limit answers at once instead of queueing without bound."""
    served.server.slots = threading.BoundedSemaphore(0)

    answer = _call({**RUN, "token": served.token})

    assert answer == [
        {
            "refused": "the launcher is busy: too many connections at once; retry, or "
            "lower the number of workers"
        }
    ]


def test_a_refusal_sent_to_a_caller_that_left_is_dropped(served: Served) -> None:
    """The write fails; nothing escapes and the next caller is served."""
    served.server.slots = threading.BoundedSemaphore(0)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
    time.sleep(0.2)
    served.server.slots = threading.BoundedSemaphore(1)

    assert _call({"op": "stop", "token": served.token, "id": "t9"}) == [
        {"result": {"stopped": False}}
    ]


def test_bytes_a_caller_sends_after_its_request_are_ignored(served: Served) -> None:
    """The watcher reads and drops them; only the end of the connection means anything."""
    engine = served.engine
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect("l.sock")
    client.sendall(json.dumps({**RUN, "token": served.token}).encode() + b"\n")
    _wait_for(lambda: sum(path.endswith("/start") for _, path, _ in engine.calls) >= 2)

    client.sendall(b"noise")
    time.sleep(0.1)
    assert engine.containers
    assert served.launcher.stop(StopRequest("t1")) == {"stopped": True}

    answer = client.makefile("rb").readline()
    client.close()
    assert json.loads(answer)["result"]["stopped"] is True


def test_serve_listens_with_the_group_mode_and_replaces_a_stale_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket is created 0660 by the umask, never changed by path afterwards."""
    monkeypatch.chdir(tmp_path)
    engine = FakeEngine()
    launcher = Launcher(settings(tmp_path), engine)
    launcher.start()
    Path("l.sock").write_text("stale", encoding="utf-8")
    seen: dict[str, object] = {}

    def listen(self: LauncherServer, poll_interval: float = 0.5) -> None:
        seen["mode"] = stat.S_IMODE(Path("l.sock").stat().st_mode)
        seen["is_socket"] = stat.S_ISSOCK(Path("l.sock").stat().st_mode)
        seen["token"] = (Path(TOKEN_FILE)).read_text(encoding="ascii")
        seen["server_token"] = self.token

    monkeypatch.setattr(LauncherServer, "serve_forever", listen)
    old = os.umask(0o022)
    try:
        serve(launcher)
        assert os.umask(0o022) == 0o022  # the umask of the process is restored
    finally:
        os.umask(old)

    assert seen["mode"] == 0o660 and seen["is_socket"] is True
    assert seen["token"] == seen["server_token"]
