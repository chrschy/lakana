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


def test_certifi_comes_last(tmp_path: Path) -> None:
    assert resolve_ca_bundle({}, (str(tmp_path / "missing.pem"),)) == certifi.where()


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
