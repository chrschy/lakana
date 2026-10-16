"""Tests for the package's top-level exports and version resolution."""

from importlib.metadata import PackageNotFoundError

import pytest

import lakana
import lakana._version


def test_version_is_resolved() -> None:
    """The installed package's version is a non-empty string."""
    assert lakana.__version__
    assert isinstance(lakana.__version__, str)


def test_version_falls_back_when_package_metadata_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve_version` falls back to a placeholder when uninstalled."""

    def _raise(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(lakana._version, "version", _raise)
    assert lakana._version.resolve_version() == "0+unknown"


def test_exports_client_and_errors() -> None:
    """The package's public API is importable from the top level."""
    assert lakana.Client is not None
    assert issubclass(lakana.NotFound, lakana.LakanaError)
    assert issubclass(lakana.ConfigError, lakana.LakanaError)
    assert issubclass(lakana.AuthenticationError, lakana.LakanaError)
    assert issubclass(lakana.PermissionDenied, lakana.LakanaError)
    assert issubclass(lakana.Conflict, lakana.LakanaError)
    assert issubclass(lakana.PayloadTooLarge, lakana.LakanaError)
    assert issubclass(lakana.ValidationError, lakana.LakanaError)
    assert issubclass(lakana.ServerError, lakana.LakanaError)
    assert issubclass(lakana.LakanaConnectionError, lakana.LakanaError)
    assert issubclass(lakana.QueryFailed, lakana.LakanaError)
    assert issubclass(lakana.InvalidResponse, lakana.LakanaError)
    assert "InvalidResponse" in lakana.__all__
