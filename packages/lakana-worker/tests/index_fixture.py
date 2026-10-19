"""Two package indexes on loopback, for tests of the credential path."""

from __future__ import annotations

import base64
import hashlib
import http.server
import io
import tarfile
import threading
import uuid
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import cast

USERNAME = "builder"
PASSWORD = "s3cr3t-Pa55word-XYZ"  # nosec B105 -- a test credential of a loopback fixture
WHEEL_NAME = "team_utils-1.4.0-py3-none-any.whl"
SDIST_NAME = "sdonly-1.0.0.tar.gz"


def _wheel() -> bytes:
    """Build the `team-utils` wheel.

    Returns
    -------
    bytes
        The wheel's content.
    """
    files = {
        "team_utils/__init__.py": b"VALUE = 42\n",
        "team_utils-1.4.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: team-utils\nVersion: 1.4.0\n"
        ),
        "team_utils-1.4.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: lakana-tests\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    record = "".join(
        f"{name},sha256="
        f"{base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},"
        f"{len(data)}\n"
        for name, data in files.items()
    )
    files["team_utils-1.4.0.dist-info/RECORD"] = (
        record + "team_utils-1.4.0.dist-info/RECORD,,\n"
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    return buffer.getvalue()


def _sdist() -> bytes:
    """Build the `sdonly` source distribution.

    Returns
    -------
    bytes
        The archive's content.
    """
    files = {
        "sdonly-1.0.0/PKG-INFO": b"Metadata-Version: 2.2\nName: sdonly\nVersion: 1.0.0\n",
        "sdonly-1.0.0/pyproject.toml": (
            b'[project]\nname = "sdonly"\nversion = "1.0.0"\n'
            b'[build-system]\nrequires = ["setuptools"]\n'
            b'build-backend = "setuptools.build_meta"\n'
        ),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


FILES: dict[str, bytes] = {WHEEL_NAME: _wheel(), SDIST_NAME: _sdist()}
"""File name → content, built once per process."""
PACKAGES: dict[str, str] = {"team-utils": WHEEL_NAME, "sdonly": SDIST_NAME}
"""Project name → its one file, for the index page `uv lock` reads."""


class _Server(http.server.ThreadingHTTPServer):
    """One index."""

    daemon_threads = True

    def __init__(self, credentials: dict[str, str]) -> None:
        """Bind a loopback port.

        Parameters
        ----------
        credentials
            Path prefix → ``user:password`` the server demands; empty demands nothing.
        """
        super().__init__(("127.0.0.1", 0), _Handler)
        self.credentials = credentials
        self.refusal = 401
        self.challenge_everything = False
        self.redirect_files_to: str | None = None
        self.seen: list[tuple[str, str | None]] = []


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serve ``<prefix>/simple/<project>/`` and ``<prefix>/files/<name>``; record every request."""

    def log_message(self, format: str, *args: object) -> None:
        """Stay silent.

        Parameters
        ----------
        format
            The base class's format string.
        *args
            Its arguments.
        """

    def _answer(
        self, status: int, body: bytes = b"", headers: dict[str, str] | None = None
    ) -> None:
        """Send a complete response.

        Parameters
        ----------
        status
            The HTTP status.
        body
            The body; not written for a HEAD request.
        headers
            Extra headers.
        """
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        """Record the request, check its credential, and serve an index page or a file."""
        server = cast("_Server", self.server)  # the base class types it as BaseServer
        given = self.headers.get("Authorization")
        server.seen.append((self.path, given))
        wanted = next(
            (pair for prefix, pair in server.credentials.items() if self.path.startswith(prefix)),
            None,
        )
        expected = None if wanted is None else "Basic " + base64.b64encode(wanted.encode()).decode()
        if server.challenge_everything or (expected is not None and given != expected):
            challenge = {"WWW-Authenticate": 'Basic realm="lakana-tests"'}
            self._answer(server.refusal, headers=challenge if server.refusal == 401 else None)
            return
        project = self.path.rstrip("/").rsplit("/", 1)[-1]
        if "/simple/" in self.path and project in PACKAGES:
            file_name = PACKAGES[project]
            digest = hashlib.sha256(FILES[file_name]).hexdigest()
            page = (
                f'<!DOCTYPE html><a href="../../files/{file_name}#sha256={digest}">{file_name}</a>'
            )
            self._answer(200, page.encode(), {"Content-Type": "text/html"})
            return
        name = self.path.rsplit("/", 1)[-1]
        if "/files/" not in self.path or name not in FILES:
            self._answer(404)
            return
        if server.redirect_files_to is not None:
            self._answer(302, headers={"Location": server.redirect_files_to + self.path})
            return
        # Real indexes send freshness headers; without one uv asks again for every build.
        self._answer(
            200,
            FILES[name],
            {"Content-Type": "application/octet-stream", "Cache-Control": "max-age=3600"},
        )

    do_HEAD = do_GET


@dataclass
class Index:
    """A running index."""

    server: _Server
    prefix: str = field(default_factory=lambda: f"/{uuid.uuid4().hex[:12]}")
    """The path in front of `/simple` in the default URL: unique per index, so that a port the
    system hands out again never gives two tests the same registered URL."""

    @property
    def port(self) -> int:
        """The loopback port."""
        return self.server.server_address[1]

    def url(self, host: str = "127.0.0.1", prefix: str | None = None) -> str:
        """Build the index URL to register or to write into a lock.

        Parameters
        ----------
        host
            The host name to write into the URL.
        prefix
            A path prefix in front of ``/simple``; the index's own unique one by default.

        Returns
        -------
        str
            The URL.
        """
        path = self.prefix if prefix is None else prefix
        return f"http://{host}:{self.port}{path}/simple"

    @property
    def credentials_received(self) -> list[str]:
        """Every credential a request carried, decoded."""
        return [
            base64.b64decode(header.split(" ", 1)[1]).decode()
            for _, header in self.server.seen
            if header is not None and header.startswith("Basic ")
        ]


@dataclass
class Indexes:
    """The two indexes of a test."""

    registered: Index
    other: Index


@contextmanager
def serving(credentials: dict[str, str] | None = None) -> Generator[Indexes, None, None]:
    """Serve the two indexes until the block ends.

    Parameters
    ----------
    credentials
        Path prefix → ``user:password`` the registered index demands; the default
        demands the fixture's pair for every path. The other index demands nothing.

    Yields
    ------
    Indexes
        The registered index and the other one.
    """
    wanted = {"": f"{USERNAME}:{PASSWORD}"} if credentials is None else credentials
    servers: list[_Server] = []
    try:
        servers.append(_Server(wanted))
        servers.append(_Server({}))
    except BaseException:
        for server in servers:
            server.server_close()
        raise
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield Indexes(Index(servers[0]), Index(servers[1]))
    finally:
        for server, thread in zip(servers, threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def project_files(index_url: str, package: str = "team-utils") -> dict[str, str]:
    """Write the two dependency files of a project that takes one package from an index.

    Parameters
    ----------
    index_url
        The index the project names for the package.
    package
        ``team-utils`` (a wheel) or ``sdonly`` (a source distribution only).

    Returns
    -------
    dict of str to str
        File name → content: ``pyproject.toml`` and ``uv.lock``.
    """
    version, file_name = ("1.4.0", WHEEL_NAME) if package == "team-utils" else ("1.0.0", SDIST_NAME)
    base = index_url.removesuffix("/simple")
    digest = hashlib.sha256(FILES[file_name]).hexdigest()
    file_entry = f'{{ url = "{base}/files/{file_name}", hash = "sha256:{digest}" }}'
    files = (
        f"wheels = [\n    {file_entry},\n]\n"
        if package == "team-utils"
        else f"sdist = {file_entry}\n"
    )
    pyproject = (
        '[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
        f'dependencies = ["{package}=={version}"]\n\n'
        f'[[tool.uv.index]]\nname = "team"\nurl = "{index_url}"\nexplicit = true\n\n'
        f'[tool.uv.sources]\n{package} = {{ index = "team" }}\n'
    )
    lock = (
        'version = 1\nrevision = 3\nrequires-python = ">=3.11"\n\n'
        '[[package]]\nname = "demo"\nversion = "0.1.0"\nsource = { virtual = "." }\n'
        f'dependencies = [\n    {{ name = "{package}" }},\n]\n\n'
        "[package.metadata]\n"
        f'requires-dist = [{{ name = "{package}", specifier = "=={version}", '
        f'index = "{index_url}" }}]\n\n'
        f'[[package]]\nname = "{package}"\nversion = "{version}"\n'
        f'source = {{ registry = "{index_url}" }}\n{files}'
    )
    return {"pyproject.toml": pyproject, "uv.lock": lock}
