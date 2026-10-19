"""Tests for the helper that removes a package index when its test ends."""

from __future__ import annotations

import pytest

from lakana.errors import NotFound
from tests.index_registration import registered_index


class _Indexes:
    """A stand-in for `admin.package_indexes` that records what it is asked."""

    def __init__(self, *, already_gone: bool = False) -> None:
        """Start with an empty registry.

        Parameters
        ----------
        already_gone
            Whether `delete` raises `NotFound`, as when the test removed the index itself.
        """
        self.rows: dict[str, str] = {}
        self.already_gone = already_gone

    def create(self, name: str, url: str, *, credential_secret: str | None = None) -> None:
        """Register an index, refusing a URL that is registered.

        Parameters
        ----------
        name
            The index name.
        url
            The index URL.
        credential_secret
            Ignored.

        Raises
        ------
        RuntimeError
            If the URL is registered already.
        """
        if url in self.rows.values():
            raise RuntimeError(f"Conflict: The package index {name!r} already has this URL")
        self.rows[name] = url

    def delete(self, name: str) -> None:
        """Remove an index.

        Parameters
        ----------
        name
            The index name.

        Raises
        ------
        NotFound
            If constructed with ``already_gone``.
        """
        if self.already_gone:
            raise NotFound("gone")
        del self.rows[name]


class _Admin:
    """A stand-in for the SDK client."""

    def __init__(self, **kwargs: bool) -> None:
        """Hold the fake registry.

        Parameters
        ----------
        **kwargs
            Passed to `_Indexes`.
        """
        self.package_indexes = _Indexes(**kwargs)


def test_two_tests_in_a_row_with_the_same_url_do_not_collide() -> None:
    """The first test's index is gone when the second registers the same URL."""
    admin = _Admin()
    url = "http://127.0.0.1:5000/simple"
    with registered_index(admin, "a", url):
        assert admin.package_indexes.rows == {"a": url}
    with registered_index(admin, "b", url):
        assert admin.package_indexes.rows == {"b": url}
    assert admin.package_indexes.rows == {}


def test_the_index_is_removed_when_the_test_fails() -> None:
    """The finalizer runs on a failure too.

    Raises
    ------
    AssertionError
        Inside the block, on purpose.
    """
    admin = _Admin()
    with pytest.raises(AssertionError), registered_index(admin, "a", "http://x/simple"):
        raise AssertionError("the test failed")
    assert admin.package_indexes.rows == {}


def test_an_index_the_test_removed_itself_is_not_an_error() -> None:
    """Removing it twice is quiet."""
    admin = _Admin(already_gone=True)
    with registered_index(admin, "a", "http://x/simple"):
        pass
