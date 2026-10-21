"""HTTPS servers on loopback with certificates of a test authority, for the query process.

The query process reads presigned HTTPS URLs through DuckDB's httpfs, which loads a CA bundle
from the local disk. A store with TLS cannot be made to play every case a test needs (a second
host, a certificate of another authority, a redirect to another server), so this module plays
them in the test process: a certificate authority made with `cryptography`, a leaf certificate
for ``127.0.0.1`` and ``localhost`` signed by it, and a threaded `http.server` whose socket is
wrapped in an `ssl.SSLContext`. `SimpleHTTPRequestHandler` answers the ``Range`` requests
DuckDB's readers make.

`make_authority` makes an authority; `serving` starts a server with a leaf of the given
authority. A test of certificate verification starts a server of one authority and gives the
query process the file of another (`make_authority` twice).
"""

from __future__ import annotations

import datetime as dt
import functools
import http.server
import ipaddress
import socket
import socketserver
import ssl
import threading
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_VALID = dt.timedelta(days=2)


@dataclass(frozen=True)
class Authority:
    """A certificate authority of a test.

    Attributes
    ----------
    name
        Its common name.
    certificate
        Its self-signed certificate.
    key
        Its private key, which signs leaf certificates.
    ca_file
        A PEM file holding the certificate only: what the query process is given as its bundle.
    """

    name: str
    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey
    ca_file: Path


def make_authority(directory: Path, name: str = "Lakana Test CA") -> Authority:
    """Make a certificate authority and write its certificate to a PEM file.

    Parameters
    ----------
    directory
        Where the PEM file is written (``<name>.pem``, spaces replaced).
    name
        The authority's common name; two authorities of one test need two names.

    Returns
    -------
    Authority
        The authority.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + _VALID)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    ca_file = directory / f"{name.replace(' ', '-')}.pem"
    ca_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return Authority(name, certificate, key, ca_file)


def _leaf(authority: Authority, directory: Path) -> tuple[Path, Path]:
    """Issue a server certificate for ``127.0.0.1`` and ``localhost`` and write it with its key.

    Parameters
    ----------
    authority
        The authority that signs it.
    directory
        Where the two PEM files are written.

    Returns
    -------
    tuple of (Path, Path)
        The certificate file and the key file.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(authority.certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + _VALID)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(authority.key, hashes.SHA256())
    )
    stem = authority.name.replace(" ", "-")
    cert_file = directory / f"{stem}-leaf.pem"
    key_file = directory / f"{stem}-leaf.key"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """A throwaway HTTPS server whose threads never outlive the test."""

    daemon_threads = True


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves files from a directory, answers listed paths with a redirect, logs nothing.

    Parameters
    ----------
    request
        The connection, as `socketserver` hands it over.
    client_address
        The client's address.
    server
        The server.
    directory
        The document root.
    redirects
        Request path to the URL its ``302`` answer names.
    seen
        Every request line is appended to it, so a test can tell what reached the server.
    """

    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: socketserver.BaseServer,
        *,
        directory: str,
        redirects: Mapping[str, str],
        seen: list[str],
    ) -> None:
        self._redirects = redirects
        self._seen = seen
        super().__init__(request, client_address, server, directory=directory)

    def _redirect(self) -> bool:
        """Answer with a redirect if the path is listed.

        Returns
        -------
        bool
            Whether a redirect was sent.
        """
        self._seen.append(f"{self.command} {self.path}")
        target = self._redirects.get(self.path.split("?", 1)[0])
        if target is None:
            return False
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def do_GET(self) -> None:
        """Answer a GET: a redirect or the file."""
        if not self._redirect():
            super().do_GET()

    def do_HEAD(self) -> None:
        """Answer a HEAD: a redirect or the file's headers."""
        if not self._redirect():
            super().do_HEAD()

    def log_message(self, format: str, *args: object) -> None:
        """Log nothing: a test's output is not the place for request lines.

        Parameters
        ----------
        format
            The format string, unused.
        *args
            Its arguments, unused.
        """


@dataclass(frozen=True)
class Serving:
    """A running HTTPS server.

    Attributes
    ----------
    base
        ``https://127.0.0.1:<port>``, without a trailing slash.
    seen
        The request lines the server received (``GET /path``), in order.
    """

    base: str
    seen: list[str]

    def url(self, path: str) -> str:
        """Name a file the server serves.

        Parameters
        ----------
        path
            The path below the served directory, without a leading slash.

        Returns
        -------
        str
            Its URL.
        """
        return f"{self.base}/{path}"


@contextmanager
def serving(
    directory: Path,
    authority: Authority,
    certificates: Path,
    redirects: Mapping[str, str] | None = None,
) -> Generator[Serving, None, None]:
    """Serve a directory over HTTPS on loopback, with a leaf certificate of `authority`.

    Parameters
    ----------
    directory
        The document root.
    authority
        Signs the server's certificate.
    certificates
        Where the leaf certificate and key are written (not inside `directory`).
    redirects
        Request path (``/a/b.parquet``) to the absolute URL a ``302`` sends the client to.

    Yields
    ------
    Serving
        The server's base URL and the requests it received.
    """
    cert_file, key_file = _leaf(authority, certificates)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    seen: list[str] = []
    handler = functools.partial(
        _Handler, directory=str(directory), redirects=dict(redirects or {}), seen=seen
    )
    server = _Server(("127.0.0.1", 0), handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Serving(f"https://127.0.0.1:{server.server_address[1]}", seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
