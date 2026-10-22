"""Tests for the CA bundle of the query process: the order it is found in, and refusals."""

from __future__ import annotations

import os
from pathlib import Path

import certifi
import pytest

from lakana_worker import ca_bundle
from lakana_worker.ca_bundle import SYSTEM_BUNDLES, CaBundleError, resolve_ca_bundle

_PEM = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def _pem(path: Path) -> Path:
    """Write a file that holds one PEM certificate block.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    Path
        `path`.
    """
    path.write_bytes(_PEM)
    return path


def test_aws_ca_bundle_comes_first(tmp_path: Path) -> None:
    aws, ssl = _pem(tmp_path / "aws.pem"), _pem(tmp_path / "ssl.pem")
    environ = {"AWS_CA_BUNDLE": str(aws), "SSL_CERT_FILE": str(ssl)}
    assert resolve_ca_bundle(environ, (str(_pem(tmp_path / "system.pem")),)) == str(aws)


def test_ssl_cert_file_comes_second(tmp_path: Path) -> None:
    ssl = _pem(tmp_path / "ssl.pem")
    environ = {"AWS_CA_BUNDLE": "", "SSL_CERT_FILE": str(ssl)}
    assert resolve_ca_bundle(environ, (str(_pem(tmp_path / "system.pem")),)) == str(ssl)


def test_the_first_readable_system_bundle_comes_third(tmp_path: Path) -> None:
    second = _pem(tmp_path / "second.pem")
    unreadable = _pem(tmp_path / "unreadable.pem")
    unreadable.chmod(0)
    try:
        candidates = (str(tmp_path / "missing.pem"), str(tmp_path), str(unreadable), str(second))
        assert resolve_ca_bundle({}, candidates) == str(second)
    finally:
        unreadable.chmod(0o600)


def test_the_first_of_several_readable_system_bundles_wins(tmp_path: Path) -> None:
    """The system list is tried in its order, as DuckDB tries it."""
    first, second = _pem(tmp_path / "first.pem"), _pem(tmp_path / "second.pem")
    assert resolve_ca_bundle({}, (str(first), str(second))) == str(first)


def test_certifi_comes_last(tmp_path: Path) -> None:
    assert resolve_ca_bundle({}, (str(tmp_path / "missing.pem"),)) == os.path.realpath(
        certifi.where()
    )


def test_a_relative_variable_is_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query process allows the path as written, and does not share the working directory."""
    _pem(tmp_path / "ca.pem")
    monkeypatch.chdir(tmp_path)
    assert resolve_ca_bundle({"SSL_CERT_FILE": "ca.pem"}, ()) == str(tmp_path / "ca.pem")


def test_the_system_list_is_duckdbs_own() -> None:
    assert SYSTEM_BUNDLES == (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/ca-bundle.pem",
        "/etc/ssl/cert.pem",
    )


@pytest.mark.parametrize("variable", ["AWS_CA_BUNDLE", "SSL_CERT_FILE"])
@pytest.mark.parametrize("problem", ["missing", "unreadable", "not PEM", "a directory"])
def test_a_variable_naming_a_file_that_cannot_serve_is_refused(
    tmp_path: Path, variable: str, problem: str
) -> None:
    path = tmp_path / "ca.pem"
    if problem == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a file of mode 000")
    if problem == "unreadable":
        _pem(path).chmod(0)
    elif problem == "not PEM":
        path.write_bytes(b"not a certificate")
    elif problem == "a directory":
        path.mkdir()
    try:
        with pytest.raises(CaBundleError) as caught:
            resolve_ca_bundle({variable: str(path)}, (certifi.where(),))
    finally:
        if path.is_file():
            path.chmod(0o600)
    assert str(caught.value) == (
        f"{variable} names {path}, which is not a readable file of PEM certificates. Point it "
        "at your object store's certificate authority, or unset it."
    )


def test_the_bundle_is_resolved_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    first = _pem(tmp_path / "first.pem")
    monkeypatch.setenv("AWS_CA_BUNDLE", str(first))
    assert ca_bundle.query_ca_bundle() == str(first)
    monkeypatch.setenv("AWS_CA_BUNDLE", str(tmp_path / "missing.pem"))
    assert ca_bundle.query_ca_bundle() == str(first)


def test_a_refusal_is_not_remembered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AWS_CA_BUNDLE", str(tmp_path / "missing.pem"))
    with pytest.raises(CaBundleError):
        ca_bundle.query_ca_bundle()
    monkeypatch.setenv("AWS_CA_BUNDLE", str(_pem(tmp_path / "ca.pem")))
    assert ca_bundle.query_ca_bundle() == str(tmp_path / "ca.pem")


def _key_block(kind: str = "PRIVATE KEY", body: str = "MIIE", end: bool = True) -> bytes:
    """Build a fake PEM key block; the markers are joined from parts, so no scanner flags them.

    Parameters
    ----------
    kind
        The label, such as ``PRIVATE KEY`` or ``EC PRIVATE KEY``.
    body
        The (fake) content line.
    end
        Whether the block has its end marker.

    Returns
    -------
    bytes
        The block.
    """
    dashes = "-" * 5
    block = f"{dashes}BEGIN {kind}{dashes}\n{body}\n"
    if end:
        block += f"{dashes}END {kind}{dashes}\n"
    return block.encode()


_KEYS = [
    "PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "DSA PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
]


@pytest.mark.parametrize("variable", ["AWS_CA_BUNDLE", "SSL_CERT_FILE"])
@pytest.mark.parametrize("kind", _KEYS)
def test_a_bundle_that_also_holds_a_private_key_is_refused(
    tmp_path: Path, variable: str, kind: str
) -> None:
    """A query past the guard could read the bundle, so it must hold certificates only."""
    path = tmp_path / "combined.pem"
    path.write_bytes(_PEM + _key_block(kind))
    with pytest.raises(CaBundleError) as caught:
        resolve_ca_bundle({variable: str(path)}, (certifi.where(),))
    assert str(caught.value) == (
        f"{variable} names {path}, which also holds a private key, and a query can read "
        f"the bundle. Point {variable} at a file of certificates only."
    )
    assert "MIIE" not in str(caught.value)


def test_a_system_bundle_that_holds_a_private_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "system.pem"
    path.write_bytes(_PEM + _key_block())
    with pytest.raises(CaBundleError) as caught:
        resolve_ca_bundle({}, (str(path),))
    assert str(caught.value) == (
        f"the system's bundle names {path}, which also holds a private key, and a query can "
        "read the bundle. Point AWS_CA_BUNDLE at a file of certificates only."
    )


def test_a_key_split_across_the_read_window_is_still_found(tmp_path: Path) -> None:
    """The whole file is searched, not a first piece of it."""
    path = tmp_path / "long.pem"
    path.write_bytes(_PEM * 300_000 + _key_block(end=False))
    with pytest.raises(CaBundleError):
        resolve_ca_bundle({"AWS_CA_BUNDLE": str(path)}, ())


def test_a_bundle_reached_through_a_symlink_is_named_by_its_real_path(tmp_path: Path) -> None:
    """DuckDB stores `ca_cert_file` and `allowed_paths` by real path (macOS: /etc → /private/etc).

    The worker names the same path, so what it logs, passes and allows is what DuckDB uses.
    """
    real = _pem(tmp_path / "real.pem")
    (tmp_path / "linked.pem").symlink_to(real)
    assert resolve_ca_bundle({"AWS_CA_BUNDLE": str(tmp_path / "linked.pem")}, ()) == str(real)
    assert resolve_ca_bundle({}, (str(tmp_path / "linked.pem"),)) == str(real)
