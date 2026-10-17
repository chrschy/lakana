"""The Docker Engine API over its Unix socket, with `http.client` alone.

Every path is prefixed with `API_VERSION`, the version the design trial ran against, so a newer
engine answers as that version did.
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode

API_VERSION = "/v1.47"
_MAX_RESPONSE = 16 * 1024 * 1024
_MAX_HEADER = 16 * 1024


class EngineError(Exception):
    """Docker could not be reached or refused a call; the text is safe to send to the worker."""


class Engine(Protocol):
    """What the launcher needs of Docker; `DockerEngine` is the real one, tests use a fake."""

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        query: dict[str, str] | None = None,
        timeout: float | None = 60.0,
    ) -> tuple[int, Any]:
        """Make one call.

        Parameters
        ----------
        method
            The HTTP method.
        path
            The path after the API version.
        body
            A JSON body, or `None`.
        query
            Query parameters.
        timeout
            Seconds; `None` waits for ever.

        Returns
        -------
        tuple
            The status and the decoded body.
        """
        ...

    def attach(self, container_id: str, *, stdin: bool) -> socket.socket:
        """Attach to a created container's streams.

        Parameters
        ----------
        container_id
            The container.
        stdin
            Whether to attach its stdin too.

        Returns
        -------
        socket.socket
            Docker's multiplexed frames out, raw bytes in.
        """
        ...


class _Connection(http.client.HTTPConnection):
    """An HTTP connection over a Unix socket.

    Parameters
    ----------
    path
        The socket.
    timeout
        Seconds for connect and each read; `None` waits for ever (``/wait``).
    """

    def __init__(self, path: str, timeout: float | None) -> None:
        super().__init__("docker", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        """Open the Unix socket instead of a TCP connection."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


class DockerEngine:
    """Calls the Engine API.

    Parameters
    ----------
    socket_path
        The Docker socket.
    timeout
        The default seconds of one call.
    """

    def __init__(self, socket_path: Path, timeout: float = 60.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _unreachable(self, exc: OSError, timeout: float | None) -> EngineError:
        """Describe a failure to talk to the socket.

        Parameters
        ----------
        exc
            The error.
        timeout
            The seconds the call was allowed.

        Returns
        -------
        EngineError
            Naming the socket and what to do.
        """
        if isinstance(exc, TimeoutError):
            return EngineError(
                f"Docker did not answer within {timeout or 0:g} s; check `docker compose logs "
                "launcher` and the load on the Docker host"
            )
        reason = exc.strerror or type(exc).__name__
        return EngineError(
            f"cannot reach Docker at {self.socket_path} ({reason}); mount the Docker socket "
            "into the launcher service"
        )

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        query: dict[str, str] | None = None,
        timeout: float | None = 60.0,
    ) -> tuple[int, Any]:
        """Make one call.

        Parameters
        ----------
        method
            The HTTP method.
        path
            The path after the version, already quoted where it holds a name.
        body
            A JSON body, or `None`.
        query
            Query parameters.
        timeout
            Seconds; `None` waits for ever.

        Returns
        -------
        tuple
            The status and the body, decoded from JSON when it is JSON, else bytes.

        Raises
        ------
        EngineError
            If the socket cannot be reached or the connection breaks.
        """
        url = API_VERSION + path + (f"?{urlencode(query)}" if query else "")
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection = _Connection(str(self.socket_path), timeout)
        try:
            connection.request(method, url, body=payload, headers=headers)
            response = connection.getresponse()
            data = response.read(_MAX_RESPONSE)
            status = response.status
        except OSError as exc:
            raise self._unreachable(exc, timeout) from None
        except http.client.HTTPException as exc:
            raise EngineError(
                f"Docker answered something that is not HTTP ({type(exc).__name__}); check "
                "`docker compose logs launcher`"
            ) from None
        finally:
            connection.close()
        if data[:1] in (b"{", b"["):
            try:
                return status, json.loads(data)
            except ValueError:
                pass
        return status, data

    def attach(self, container_id: str, *, stdin: bool) -> socket.socket:
        """Attach to a created container's streams, before it starts.

        Parameters
        ----------
        container_id
            The container.
        stdin
            Whether to attach its stdin too.

        Returns
        -------
        socket.socket
            The upgraded connection: Docker's multiplexed frames out, raw bytes in.

        Raises
        ------
        EngineError
            If the socket cannot be reached or Docker does not upgrade.
        """
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stream.settimeout(self.timeout)
            stream.connect(str(self.socket_path))
            query = f"stream=1&stdout=1&stderr=1&stdin={1 if stdin else 0}"
            target = f"{API_VERSION}/containers/{quote(container_id, safe='')}/attach?{query}"
            stream.sendall(
                f"POST {target} HTTP/1.1\r\n"
                "Host: docker\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n".encode("ascii")
            )
            head = b""
            # One byte at a time: the first frame may follow the headers in the same packet.
            while b"\r\n\r\n" not in head and len(head) < _MAX_HEADER:
                byte = stream.recv(1)
                if not byte:
                    break
                head += byte
        except OSError as exc:
            stream.close()
            raise self._unreachable(exc, self.timeout) from None
        if b"\r\n\r\n" not in head or b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            stream.close()
            raise EngineError(
                "Docker did not attach to the container; check `docker compose logs launcher`"
            )
        stream.settimeout(None)
        return stream
