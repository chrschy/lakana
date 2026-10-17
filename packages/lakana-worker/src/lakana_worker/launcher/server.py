"""The launcher's Unix socket: one request per connection, authenticated by a shared token.

The socket and the token file sit in a volume only the worker mounts. The directory belongs to
the worker's group with the set-group-id bit, so both files belong to that group without the
launcher needing a capability to change owners, and with the sticky bit, so the worker can
neither rename nor delete them. Both files get their mode while they are being created (the
umask for the socket, `fchmod` on the open token file): no path is ever changed by name, so a
link planted in the directory cannot redirect it.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import secrets
import socket
import socketserver
import threading
from collections.abc import Callable
from pathlib import Path

from lakana_worker.launcher.engine import EngineError
from lakana_worker.launcher.requests import (
    MAX_REQUEST_BYTES,
    BuildRequest,
    Refused,
    Request,
    RunRequest,
    container_name,
    parse_request,
)
from lakana_worker.launcher.service import Launcher

logger = logging.getLogger("lakana_worker.launcher")

TOKEN_FILE = "launcher.token"
_MAX_CONNECTIONS = 128
_READ_TIMEOUT = 30.0
"""Seconds a connection has to send its request line; the wait for the result is not bounded."""
_SOCKET_UMASK = 0o117
"""Leaves the socket 0660: the owner and the worker's group may connect."""


class _CallerGone(Exception):
    """The caller closed its connection; its run is cleaned up and nothing can be answered."""


def write_token(directory: Path) -> str:
    """Write a fresh token next to the socket, readable by the owner and the group only.

    Parameters
    ----------
    directory
        The socket's directory.

    Returns
    -------
    str
        The token (64 hex digits).
    """
    token = secrets.token_hex(32)
    temporary = directory / f".{TOKEN_FILE}.{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        os.fchmod(handle.fileno(), 0o640)
        handle.write(token)
    os.replace(temporary, directory / TOKEN_FILE)
    return token


def _watch(connection: socket.socket, done: threading.Event, on_gone: Callable[[], None]) -> None:
    """Call `on_gone` when the caller closes its connection before the request is done.

    A caller sends nothing after its request line, so `recv` returns only at the end.

    Parameters
    ----------
    connection
        The caller's connection.
    done
        Set when the request has been answered; a close after that is not a departure.
    on_gone
        What to do then.
    """
    with contextlib.suppress(OSError):
        while connection.recv(4096):
            pass
    if not done.is_set():
        on_gone()


class _Handler(socketserver.StreamRequestHandler):
    """Serves one connection."""

    server: LauncherServer

    def _send(self, answer: dict[str, object]) -> None:
        """Write one answer line.

        Parameters
        ----------
        answer
            The line's object.

        Raises
        ------
        _CallerGone
            If the caller has closed its connection.
        """
        try:
            self.wfile.write(json.dumps(answer).encode("utf-8") + b"\n")
            self.wfile.flush()
        except OSError:
            raise _CallerGone from None

    def _try_send(self, answer: dict[str, object]) -> None:
        """Write one answer line, leaving a caller that has gone unanswered.

        Parameters
        ----------
        answer
            The line's object.
        """
        with contextlib.suppress(_CallerGone):
            self._send(answer)

    def handle(self) -> None:
        """Read the request, run it, answer; never let an error escape."""
        if not self.server.slots.acquire(blocking=False):
            # Read the request first: closing a socket with unread bytes resets the connection
            # and the caller would never see the refusal.
            self.connection.settimeout(1.0)
            with contextlib.suppress(OSError):
                self.rfile.readline(MAX_REQUEST_BYTES + 1)
            self._try_send(
                {
                    "refused": "the launcher is busy: too many connections at once; retry, or "
                    "lower the number of workers"
                }
            )
            return
        try:
            self._serve()
        finally:
            self.server.slots.release()

    def _read_line(self) -> bytes | None:
        """Read the request line, bounded in size and in time.

        Returns
        -------
        bytes or None
            The line, or `None` when none arrived in time (the caller was told).
        """
        self.connection.settimeout(_READ_TIMEOUT)
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        except OSError:
            self._try_send({"refused": "no request line arrived in time; send the request at once"})
            return None
        self.connection.settimeout(None)
        return raw

    def _serve(self) -> None:
        """Parse, supervise, answer."""
        launcher = self.server.launcher
        raw = self._read_line()
        if raw is None:
            return
        try:
            request = parse_request(raw, token=self.server.token, settings=launcher.settings)
        except Refused as exc:
            logger.warning("Refused a request: %s", exc)
            self._try_send({"refused": str(exc)})
            return
        name: str | None = None
        done = threading.Event()
        if isinstance(request, (RunRequest, BuildRequest)):
            kind, ident = (
                ("task", request.task)
                if isinstance(request, RunRequest)
                else ("build", request.build)
            )
            name = container_name(launcher.settings, kind, ident)
            threading.Thread(
                target=_watch,
                args=(self.connection, done, functools.partial(launcher.abandon, name)),
                daemon=True,
            ).start()
        try:
            self._answer(request)
        finally:
            done.set()
            if name is not None:
                # A departure that landed just before `done` was set is spent here.
                launcher.forget(name)

    def _answer(self, request: Request) -> None:
        """Run a parsed request and send its result, or say what went wrong.

        Parameters
        ----------
        request
            The parsed request.
        """
        try:
            result = self.server.launcher.handle(request, self._send)
        except _CallerGone:
            return  # the run is cleaned up already
        except EngineError as exc:
            logger.error("Docker failed: %s", exc)
            self._try_send({"error": str(exc)})
            return
        except Exception:
            logger.exception("The launcher failed while serving a request")
            self._try_send({"error": "launcher internal error; see docker compose logs launcher"})
            return
        self._try_send({"result": result})


class LauncherServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """The launcher's socket server.

    Parameters
    ----------
    path
        Where to listen.
    launcher
        Runs the requests.
    token
        What every request must carry.
    """

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, path: str, *, launcher: Launcher, token: str) -> None:
        self.launcher = launcher
        self.token = token
        self.slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        # The socket is created with its final mode: the umask leaves it 0660.
        previous = os.umask(_SOCKET_UMASK)
        try:
            super().__init__(path, _Handler)
        finally:
            os.umask(previous)


def serve(launcher: Launcher) -> None:
    """Listen on the configured socket until the process ends.

    Parameters
    ----------
    launcher
        The started launcher.
    """
    path = launcher.settings.socket
    token = write_token(path.parent)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    server = LauncherServer(str(path), launcher=launcher, token=token)
    logger.info("The launcher listens on %s.", path)
    server.serve_forever()
