"""The certificate authorities the query process trusts for HTTPS reads.

DuckDB's httpfs reads its CA bundle from the local disk, lazily, on the first HTTPS request. In
allow-list mode the query process may open only what the worker names, so the worker names one
PEM file: it becomes the connection's ``ca_cert_file`` and its only ``allowed_paths`` entry.

The file is resolved once, the first time it is asked for (the worker asks at start-up and logs
the path, never the content):

1. ``AWS_CA_BUNDLE``, if set: what an operator of an S3-compatible store with a private
   certificate authority already sets for the worker's own S3 client.
2. ``SSL_CERT_FILE``, if set.
3. The system bundle: the first readable file of `SYSTEM_BUNDLES` (DuckDB 2.0's own list). It
   keeps a corporate CA installed into the system store working.
4. `certifi.where()`: Mozilla's bundle, shipped with the worker.

A variable that names a missing, unreadable or non-PEM file stops the worker at start-up
(`CaBundleError`) instead of failing every HTTPS read later. No setting of Lakana's own exists
for this; see ``docs/site/reference/network-settings.md``.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Mapping, Sequence

import certifi

VARIABLES = ("AWS_CA_BUNDLE", "SSL_CERT_FILE")
"""The environment variables consulted, in order."""

SYSTEM_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)
"""DuckDB 2.0's own list of system bundles (Debian/Ubuntu, Fedora/RHEL, openSUSE, Alpine, macOS),
as its refusal to read one names them."""

_PEM_MARK = b"-----BEGIN CERTIFICATE-----"
_READ_LIMIT = 16 * 1024 * 1024
"""Bytes of a bundle read to find a certificate (a bundle of every public CA is about 200 kB)."""


class CaBundleError(ValueError):
    """A variable names a file the query process cannot use as its CA bundle."""


def _message(variable: str, path: str) -> str:
    """Word the refusal of a variable's file.

    Parameters
    ----------
    variable
        The environment variable.
    path
        The path it names.

    Returns
    -------
    str
        What the worker says before it stops.
    """
    return (
        f"{variable} names {path}, which is not a readable file of PEM certificates. Point it "
        "at your object store's certificate authority, or unset it."
    )


def _is_pem_file(path: str) -> bool:
    """Tell whether a path is a readable file holding at least one PEM certificate.

    Parameters
    ----------
    path
        The path.

    Returns
    -------
    bool
        Whether the file can be read and holds ``-----BEGIN CERTIFICATE-----``.
    """
    try:
        with open(path, "rb") as bundle:
            return _PEM_MARK in bundle.read(_READ_LIMIT)
    except OSError:
        return False


def find_ca_bundle(
    environ: Mapping[str, str] | None = None,
    system_bundles: Sequence[str] = SYSTEM_BUNDLES,
    fallback: Callable[[], str] = certifi.where,
) -> tuple[str, str]:
    """Find the CA bundle of the query process and say where it came from.

    See the module's docstring for the order.

    Parameters
    ----------
    environ
        The environment; the worker's own (`os.environ`) by default.
    system_bundles
        The system paths tried after the variables.
    fallback
        Names the bundle used when nothing else exists.

    Returns
    -------
    tuple of (str, str)
        The absolute path of the bundle (a relative variable is made absolute against the
        worker's working directory, because the query process allows the path as written), and
        its source for the start-up log: the variable's name, ``the system's bundle`` or
        ``certifi``.

    Raises
    ------
    CaBundleError
        If a set variable names a missing, unreadable or non-PEM file. An empty variable counts
        as not set.
    """
    environment = os.environ if environ is None else environ
    for variable in VARIABLES:
        value = environment.get(variable, "")
        if value:
            if not _is_pem_file(value):
                raise CaBundleError(_message(variable, value))
            return os.path.abspath(value), variable
    for candidate in system_bundles:
        if os.path.isfile(candidate) and os.access(candidate, os.R_OK):
            return candidate, "the system's bundle"
    return os.path.abspath(fallback()), "certifi"


def resolve_ca_bundle(
    environ: Mapping[str, str] | None = None,
    system_bundles: Sequence[str] = SYSTEM_BUNDLES,
    fallback: Callable[[], str] = certifi.where,
) -> str:
    """Find the CA bundle of the query process (`find_ca_bundle` without the source).

    Parameters
    ----------
    environ
        As for `find_ca_bundle`.
    system_bundles
        As for `find_ca_bundle`.
    fallback
        As for `find_ca_bundle`.

    Returns
    -------
    str
        The absolute path of the bundle.

    Raises
    ------
    CaBundleError
        As `find_ca_bundle`.
    """
    return find_ca_bundle(environ, system_bundles, fallback)[0]


@functools.cache
def query_ca_bundle() -> str:
    """Resolve the CA bundle once for the life of the worker.

    Returns
    -------
    str
        What `resolve_ca_bundle` returned the first time.

    Raises
    ------
    CaBundleError
        As `resolve_ca_bundle`; nothing is cached then, so the worker's start-up check raises it
        before any query runs.
    """
    return resolve_ca_bundle()
