"""The worker's side of the launcher's socket.

One request per connection, the launcher's token added from the file next to the socket. Every
failure to reach the launcher, and every Docker failure it reports, becomes
`LauncherUnavailable` with the task's message: "runner unavailable", the launcher service and
`LAKANA_RUN__LAUNCHER_SOCKET`. A refusal is `LauncherRefused`: the request itself was wrong.
Whatever the launcher answers is read as untrusted input: an answer outside the protocol (not
JSON, nested too deeply, a log that is not base64, a line longer than 1 MiB) is the launcher
being unavailable, never another exception.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import socket
from collections.abc import Callable
from pathlib import Path

from lakana_worker.launcher.server import TOKEN_FILE

_MAX_LINE = 2 << 20
"""Longest answer line: the launcher sends log pieces of up to 1 MiB, ~1.4 MB as base64 JSON."""
_MAX_TOKEN_BYTES = 4096
_UNSAFE_OWNER = re.compile(r"[^A-Za-z0-9._:-]")
_NOT_PROTOCOL = "the launcher answered something that is not its protocol"
_TRANSIENT_REFUSALS = ("the launcher is busy:", "no request line arrived in time")
"""Refusals that say the launcher could not take the request now, not that it was wrong."""


class LauncherUnavailable(Exception):
    """The launcher cannot be reached, or Docker failed; the text is the task's message."""


class LauncherRefused(Exception):
    """The launcher refused the request; the text is its reason."""


class LogSinkFailed(Exception):
    """The caller's log sink raised while output arrived."""


def unavailable_message(detail: str, socket_path: Path) -> str:
    """Write the "runner unavailable" message.

    Parameters
    ----------
    detail
        What went wrong.
    socket_path
        `LAKANA_RUN__LAUNCHER_SOCKET`.

    Returns
    -------
    str
        The cause and the next step.
    """
    return (
        f"runner unavailable: {detail}. Check that the launcher service is running (docker "
        f"compose ps launcher) and that LAKANA_RUN__LAUNCHER_SOCKET ({socket_path}) is the "
        "socket in its volume."
    )


def launcher_owner(host: str) -> str:
    """Name this worker for the launcher's labels: its host name, made safe.

    A Compose replica keeps its host name across a restart of its container, so a restarted
    worker's `cleanup` finds what the earlier process left.

    Parameters
    ----------
    host
        `socket.gethostname()`.

    Returns
    -------
    str
        Letters, digits, ``.``, ``_``, ``:`` and ``-``, starting with a letter or digit, at most
        128 characters.
    """
    owner = _UNSAFE_OWNER.sub("-", host)[:127]
    if not owner:
        return "worker"
    return owner if owner[0].isalnum() else f"w{owner}"


class LauncherClient:
    """Sends requests to the launcher.

    Parameters
    ----------
    socket_path
        `LAKANA_RUN__LAUNCHER_SOCKET`.
    connect_timeout
        Seconds to connect and to send the request; reading the answers has no timeout, since
        a task may be silent for as long as it runs (the launcher ends it at its timeout).
    """

    def __init__(self, socket_path: Path, *, connect_timeout: float = 10.0) -> None:
        self.socket_path = socket_path
        self.connect_timeout = connect_timeout

    def _unavailable(self, detail: str) -> LauncherUnavailable:
        """Build the exception for a failure to reach the launcher.

        Parameters
        ----------
        detail
            What went wrong.

        Returns
        -------
        LauncherUnavailable
            With `unavailable_message`.
        """
        return LauncherUnavailable(unavailable_message(detail, self.socket_path))

    def _token(self) -> str:
        """Read the launcher's token from the file next to its socket.

        Returns
        -------
        str
            The token.

        Raises
        ------
        LauncherUnavailable
            If the file cannot be read or is not a short ASCII text.
        """
        try:
            with (self.socket_path.parent / TOKEN_FILE).open("rb") as handle:
                data = handle.read(_MAX_TOKEN_BYTES + 1)
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise self._unavailable(f"the launcher's token cannot be read ({reason})") from None
        if len(data) > _MAX_TOKEN_BYTES or not data.isascii():
            raise self._unavailable(
                "the launcher's token cannot be read (not the launcher's token)"
            )
        return data.decode("ascii").strip()

    def call(
        self, request: dict[str, object], on_log: Callable[[bytes], None] | None = None
    ) -> dict[str, object]:
        """Send one request and read its answers to the result.

        Parameters
        ----------
        request
            The request without the token.
        on_log
            Receives each log chunk; `None` drops them.

        Returns
        -------
        dict
            The result line's value.

        Raises
        ------
        LauncherUnavailable
            If the token or the socket cannot be reached, the connection breaks, the answer is
            not the protocol, or Docker failed.
        LauncherRefused
            If the launcher refused the request; a busy launcher or a request line that came
            too late is `LauncherUnavailable` instead, since a retry may succeed.
        LogSinkFailed
            If `on_log` raised.
        ValueError
            Never escapes: an answer outside the protocol becomes `LauncherUnavailable`.
        """
        token = self._token()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.connect_timeout)
            connection.connect(str(self.socket_path))
            connection.sendall(json.dumps({**request, "token": token}).encode("utf-8") + b"\n")
            connection.settimeout(None)
            with connection.makefile("rb") as reader:
                while line := reader.readline(_MAX_LINE):
                    answer = json.loads(line)
                    if not isinstance(answer, dict):
                        raise ValueError(_NOT_PROTOCOL)
                    if "log" in answer:
                        chunk = base64.b64decode(str(answer["log"]), validate=True)
                        if on_log is not None:
                            try:
                                on_log(chunk)
                            except Exception as exc:  # the caller's sink, reported as such
                                raise LogSinkFailed("the log sink failed") from exc
                    elif "result" in answer and isinstance(answer["result"], dict):
                        return answer["result"]
                    elif "refused" in answer:
                        reason = str(answer["refused"])
                        if reason.startswith(_TRANSIENT_REFUSALS):
                            raise LauncherUnavailable(f"runner unavailable: {reason}")
                        raise LauncherRefused(reason)
                    elif "error" in answer:
                        raise self._unavailable(f"Docker failed: {answer['error']}")
                    else:
                        raise ValueError(_NOT_PROTOCOL)
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise self._unavailable(f"the launcher's socket cannot be reached ({reason})") from None
        except (ValueError, binascii.Error, RecursionError):
            raise self._unavailable(_NOT_PROTOCOL) from None
        finally:
            connection.close()
        raise self._unavailable("the launcher closed the connection without an answer")

    def stop(self, ident: str) -> bool:
        """Ask the launcher to stop a task or build.

        Parameters
        ----------
        ident
            Its id.

        Returns
        -------
        bool
            Whether it was running.
        """
        return self.call({"op": "stop", "id": ident}).get("stopped") is True

    def status(self, python: str, key: str) -> bool:
        """Ask whether an environment is built.

        Parameters
        ----------
        python
            The Python version.
        key
            The environment key.

        Returns
        -------
        bool
            Whether it is complete.
        """
        answer = self.call({"op": "status", "python": python, "environment": key})
        return answer.get("complete") is True

    def cleanup(self, owner: str) -> int:
        """Remove what this worker left behind before it restarted.

        Parameters
        ----------
        owner
            `launcher_owner`'s answer.

        Returns
        -------
        int
            How many containers were removed.
        """
        removed = self.call({"op": "cleanup", "owner": owner}).get("removed")
        return removed if type(removed) is int else 0
