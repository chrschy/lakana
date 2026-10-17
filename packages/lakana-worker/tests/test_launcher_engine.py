"""`DockerEngine`: the pinned API version over a Unix socket, and its errors."""

from __future__ import annotations

import contextlib
import http.server
import json
import socketserver
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from lakana_worker.launcher.engine import API_VERSION, DockerEngine, EngineError


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers like Docker: JSON for GET and POST, a 101 upgrade for attach."""

    seen: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: object) -> None:
        """Stay quiet.

        Parameters
        ----------
        format
            Ignored.
        *args
            Ignored.
        """

    def do_GET(self) -> None:
        """Answer ``GET /version``."""
        type(self).seen.append(self.path)
        if self.path.endswith("/broken"):
            body = b"{not json"
        elif self.path.endswith("/plain"):
            body = b"OK"
        else:
            body = json.dumps({"Version": "29.8.0"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """Answer an attach with an upgrade and one frame."""
        type(self).seen.append(self.path)
        if "/nope/" in self.path:
            self.wfile.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return
        self.wfile.write(b"HTTP/1.1 101 UPGRADED\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
        self.wfile.write(b"\x01\x00\x00\x00\x00\x00\x00\x03hi\n")
        self.wfile.flush()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """A Unix-socket HTTP server."""

    daemon_threads = True


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Serve the fake on a short relative path.

    Parameters
    ----------
    tmp_path
        The working directory.
    monkeypatch
        Changes it.

    Yields
    ------
    Path
        The socket.
    """
    monkeypatch.chdir(tmp_path)
    server = _Server("d.sock", _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Path("d.sock")
    server.shutdown()
    server.server_close()


def test_requests_go_to_the_pinned_api_version_and_decode_json(docker: Path) -> None:
    """Every path starts with `/v1.47`, the version the design trial ran against."""
    status, body = DockerEngine(docker).request("GET", "/version")

    assert (status, body) == (200, {"Version": "29.8.0"})
    assert _Handler.seen[-1] == f"{API_VERSION}/version" == "/v1.47/version"


def test_attach_returns_the_raw_stream_after_the_upgrade(docker: Path) -> None:
    """Nothing of the stream is read past the headers."""
    stream = DockerEngine(docker).attach("c1", stdin=False)
    data = b""
    while chunk := stream.recv(4096):
        data += chunk
    stream.close()

    assert data == b"\x01\x00\x00\x00\x00\x00\x00\x03hi\n"
    assert _Handler.seen[-1].startswith("/v1.47/containers/c1/attach?stream=1")


def test_an_unreachable_socket_names_the_socket_and_the_mount(tmp_path: Path) -> None:
    """The launcher's first log line says what to fix."""
    with pytest.raises(EngineError, match="mount the Docker socket into the launcher service"):
        DockerEngine(tmp_path / "missing.sock").request("GET", "/version")


def test_a_body_that_is_not_json_comes_back_as_bytes(docker: Path) -> None:
    """Pull progress and `/_ping` are not JSON; a truncated JSON body is not an error either."""
    engine = DockerEngine(docker)

    assert engine.request("GET", "/plain") == (200, b"OK")
    assert engine.request("GET", "/broken") == (200, b"{not json")


def test_a_query_is_encoded_after_the_version(docker: Path) -> None:
    """Filters are JSON in a query value."""
    DockerEngine(docker).request("GET", "/containers/json", query={"filters": '{"label": ["a=b"]}'})

    assert (
        _Handler.seen[-1] == "/v1.47/containers/json?filters=%7B%22label%22%3A+%5B%22a%3Db%22%5D%7D"
    )


def test_a_missing_container_is_not_attached(docker: Path) -> None:
    """Only a 101 upgrade counts as attached."""
    with pytest.raises(EngineError, match="Docker did not attach to the container; check"):
        DockerEngine(docker).attach("nope", stdin=True)


def test_attach_asks_for_stdin_only_when_the_caller_writes_to_it(docker: Path) -> None:
    """A build container has no stdin."""
    DockerEngine(docker).attach("c1", stdin=True).close()

    assert "stdin=1" in _Handler.seen[-1]


def test_attach_to_an_unreachable_socket_names_the_mount(tmp_path: Path) -> None:
    """Same message as a request."""
    with pytest.raises(EngineError, match="mount the Docker socket into the launcher service"):
        DockerEngine(tmp_path / "missing.sock").attach("c1", stdin=False)


def _raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: bytes, delay: float = 0.0) -> Path:
    """Serve one fixed reply to every connection on a short relative path.

    Parameters
    ----------
    tmp_path
        The working directory.
    monkeypatch
        Changes it.
    reply
        The bytes to send before closing.
    delay
        Seconds to wait before sending them.

    Returns
    -------
    Path
        The socket.
    """
    monkeypatch.chdir(tmp_path)

    class Raw(socketserver.StreamRequestHandler):
        """Sends `reply` and hangs up."""

        def handle(self) -> None:
            """Read the request line, send the reply."""
            self.rfile.readline()
            time.sleep(delay)
            with contextlib.suppress(OSError):
                self.wfile.write(reply)

    server = _Server("r.sock", Raw)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return Path("r.sock")


def test_an_answer_that_is_not_http_names_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Something else listening on the socket is a configuration error the operator can find."""
    path = _raw(tmp_path, monkeypatch, b"garbage\r\n\r\n")

    with pytest.raises(EngineError, match=r"not HTTP .*docker compose logs launcher"):
        DockerEngine(path).request("GET", "/version")


def test_headers_that_never_end_are_not_read_without_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that claims to upgrade but sends headers for ever is cut at 16 KiB."""
    path = _raw(tmp_path, monkeypatch, b"HTTP/1.1 101 UPGRADED\r\n" + b"X: y\r\n" * 5000)

    with pytest.raises(EngineError, match="did not attach"):
        DockerEngine(path).attach("c1", stdin=False)


def test_a_connection_that_closes_inside_the_headers_is_not_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end of the stream before the blank line."""
    path = _raw(tmp_path, monkeypatch, b"HTTP/1.1 101 UPGRADED\r\n")

    with pytest.raises(EngineError, match="did not attach"):
        DockerEngine(path).attach("c1", stdin=False)


def test_a_read_timeout_says_the_engine_did_not_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow pull is not a missing socket: the next step differs."""
    path = _raw(tmp_path, monkeypatch, b"HTTP/1.1 200 OK\r\n\r\n", delay=1.0)

    with pytest.raises(EngineError, match=r"did not answer within 0\.2 s") as caught:
        DockerEngine(path).request("GET", "/version", timeout=0.2)

    assert "mount the Docker socket" not in str(caught.value)


def test_an_attach_that_times_out_says_the_engine_did_not_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same message when the upgrade never comes."""
    path = _raw(tmp_path, monkeypatch, b"HTTP/1.1 101 UPGRADED\r\n\r\n", delay=1.0)

    with pytest.raises(EngineError, match=r"did not answer within 0\.2 s"):
        DockerEngine(path, timeout=0.2).attach("c1", stdin=False)
