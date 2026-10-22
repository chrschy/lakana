"""`LauncherClient`: the token, the answer lines, and what each failure becomes."""

from __future__ import annotations

import base64
import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lakana_worker.launcher_client import (
    LauncherClient,
    LauncherRefused,
    LauncherUnavailable,
    LogSinkFailed,
    launcher_owner,
    unavailable_message,
)


@dataclass
class Answering:
    """A one-connection-at-a-time launcher stand-in.

    Parameters
    ----------
    answers
        The bytes to send on the next connection, in order.
    received
        The request line of each connection.
    """

    answers: list[bytes] = field(default_factory=list)
    received: list[bytes] = field(default_factory=list)


@pytest.fixture
def answering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Answering]:
    """Serve one scripted answer per connection on a short relative socket path.

    Parameters
    ----------
    tmp_path
        The working directory.
    monkeypatch
        Changes it.

    Yields
    ------
    Answering
        Append answers; read the request lines.
    """
    monkeypatch.chdir(tmp_path)
    Path("launcher.token").write_text("t" * 64)
    state = Answering()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind("l.sock")
    server.listen(4)

    def serve() -> None:
        """Answer connections until the socket closes."""
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection, connection.makefile("rb") as reader:
                state.received.append(reader.readline())
                connection.sendall(state.answers.pop(0))

    threading.Thread(target=serve, daemon=True).start()
    yield state
    server.close()


def test_the_client_sends_the_token_streams_logs_and_returns_the_result(
    answering: Answering,
) -> None:
    """Logs go to the sink as bytes; the result line ends the call."""
    answering.answers.append(
        json.dumps({"log": base64.b64encode(b"hi\n").decode()}).encode()
        + b"\n"
        + json.dumps({"result": {"complete": True}}).encode()
        + b"\n"
    )
    logs: list[bytes] = []

    result = LauncherClient(Path("l.sock")).call({"op": "status"}, logs.append)

    assert result == {"complete": True} and logs == [b"hi\n"]
    sent = json.loads(answering.received[0])
    assert sent == {"op": "status", "token": "t" * 64}


def test_logs_without_a_sink_are_dropped(answering: Answering) -> None:
    """`on_log=None` reads past the logs to the result."""
    answering.answers.append(
        json.dumps({"log": base64.b64encode(b"x").decode()}).encode()
        + b'\n{"result": {"removed": 2}}\n'
    )

    assert LauncherClient(Path("l.sock")).cleanup("worker-1") == 2
    assert json.loads(answering.received[0]) == {
        "op": "cleanup",
        "owner": "worker-1",
        "token": "t" * 64,
    }


def test_a_refusal_and_an_error_become_their_exceptions(answering: Answering) -> None:
    """`refused` is the launcher's verdict; `error` is Docker failing: runner unavailable."""
    answering.answers.append(
        b'{"refused": "op must be one of build, cleanup, run, status, stop"}\n'
    )
    answering.answers.append(b'{"error": "the server service of this stack is not running"}\n')
    client = LauncherClient(Path("l.sock"))

    with pytest.raises(LauncherRefused, match="op must be one of"):
        client.call({"op": "x"})
    with pytest.raises(LauncherUnavailable) as caught:
        client.call({"op": "run"})
    assert str(caught.value) == unavailable_message(
        "Docker failed: the server service of this stack is not running", Path("l.sock")
    )


@pytest.mark.parametrize(
    "answer",
    [
        b"not json\n",
        b"[1]\n",
        b'{"log": "%%%"}\n',
        b'{"result": 3}\n',
        b'{"other": 1}\n',
        b"[" * 100_000 + b"\n",
    ],
    ids=["text", "list", "bad-base64", "result-not-object", "unknown", "deep-nesting"],
)
def test_an_answer_outside_the_protocol_is_runner_unavailable(
    answering: Answering, answer: bytes
) -> None:
    """Nothing the launcher sends escapes as another exception, deep nesting included.

    Parameters
    ----------
    answering
        The stand-in.
    answer
        What it sends.
    """
    answering.answers.append(answer)

    with pytest.raises(LauncherUnavailable, match="not its protocol"):
        LauncherClient(Path("l.sock")).call({"op": "status"})


def test_a_connection_closed_without_a_result_is_runner_unavailable(
    answering: Answering,
) -> None:
    """The launcher went away in the middle of the call."""
    answering.answers.append(b"")

    with pytest.raises(LauncherUnavailable, match="closed the connection without an answer"):
        LauncherClient(Path("l.sock")).call({"op": "status"})


def test_a_missing_socket_is_runner_unavailable_naming_the_service_and_the_setting(
    tmp_path: Path,
) -> None:
    """The spec's message: the launcher service and LAKANA_RUN__LAUNCHER_SOCKET."""
    path = tmp_path / "nowhere" / "launcher.sock"

    with pytest.raises(LauncherUnavailable) as caught:
        LauncherClient(path).call({"op": "status"})

    text = str(caught.value)
    assert text.startswith("runner unavailable: the launcher's token cannot be read")
    assert "docker compose ps launcher" in text and f"LAKANA_RUN__LAUNCHER_SOCKET ({path})" in text


def test_a_token_without_a_listening_socket_is_runner_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token is there, the launcher is not."""
    monkeypatch.chdir(tmp_path)
    Path("launcher.token").write_text("t" * 64)

    with pytest.raises(LauncherUnavailable, match="socket cannot be reached"):
        LauncherClient(Path("l.sock")).call({"op": "status"})


@pytest.mark.parametrize("content", [b"t" * 5000, b"\xff" * 64], ids=["huge", "not-ascii"])
def test_a_token_file_that_is_not_a_token_is_runner_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    """The file is read bounded, and only as ASCII.

    Parameters
    ----------
    tmp_path
        The working directory.
    monkeypatch
        Changes it.
    content
        What the token file holds.
    """
    monkeypatch.chdir(tmp_path)
    Path("launcher.token").write_bytes(content)

    with pytest.raises(LauncherUnavailable, match=r"token cannot be read \(not the launcher's"):
        LauncherClient(Path("l.sock")).call({"op": "status"})


def test_a_sink_that_raises_is_reported_as_the_sink_failing(answering: Answering) -> None:
    """Not as the launcher being unavailable: the runner must stop the container."""
    answering.answers.append(json.dumps({"log": base64.b64encode(b"x").decode()}).encode() + b"\n")

    def broken(chunk: bytes) -> None:
        """Fail like a full disk.

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

    with pytest.raises(LogSinkFailed):
        LauncherClient(Path("l.sock")).call({"op": "run"}, broken)


def test_stop_and_status_read_their_answers(answering: Answering) -> None:
    """Anything but `true` is `False`."""
    answering.answers += [
        b'{"result": {"stopped": true}}\n',
        b'{"result": {"stopped": 1}}\n',
        b'{"result": {"complete": true}}\n',
        b'{"result": {"removed": "2"}}\n',
    ]
    client = LauncherClient(Path("l.sock"))

    assert client.stop("abc") is True
    assert client.stop("abc") is False
    assert client.status("3.13", "e" * 64) is True
    assert client.cleanup("worker-1") == 0
    assert json.loads(answering.received[0]) == {"op": "stop", "id": "abc", "token": "t" * 64}
    assert json.loads(answering.received[2]) == {
        "op": "status",
        "python": "3.13",
        "environment": "e" * 64,
        "token": "t" * 64,
    }


@pytest.mark.parametrize(
    ("host", "owner"),
    [
        ("worker-1", "worker-1"),
        ("Box.local", "Box.local"),
        ("a b/c", "a-b-c"),
        ("-x", "w-x"),
        ("", "worker"),
        ("x" * 300, "x" * 127),
    ],
)
def test_the_owner_is_the_host_name_made_safe(host: str, owner: str) -> None:
    """The launcher's owner label stays stable across a restart of the same container.

    Parameters
    ----------
    host
        The host name.
    owner
        The owner the launcher gets.
    """
    assert launcher_owner(host) == owner
