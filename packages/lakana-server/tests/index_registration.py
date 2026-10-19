"""Register a package index for the length of one test."""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from typing import Protocol

from lakana.errors import NotFound


class _Indexes(Protocol):
    """The part of the SDK's package index API this module uses."""

    def create(self, name: str, url: str, *, credential_secret: str | None = None) -> object:
        """Register an index.

        Parameters
        ----------
        name
            The index's name.
        url
            The index's URL.
        credential_secret
            The secret that holds the index's credential, if it needs one.
        """

    def delete(self, name: str) -> object:
        """Remove an index.

        Parameters
        ----------
        name
            The index's name.
        """


class IndexAdmin(Protocol):
    """An SDK client that manages package indexes."""

    @property
    def package_indexes(self) -> _Indexes:
        """The package index API."""


@contextlib.contextmanager
def registered_index(
    admin: IndexAdmin, name: str, url: str, credential_secret: str | None = None
) -> Generator[None, None, None]:
    """Register a package index and remove it when the block ends, also on a failure.

    Indexes live on a server that outlives the test; one left behind keeps its URL, and a later
    test that is handed the same URL fails with a conflict.

    Parameters
    ----------
    admin
        The SDK client of an administrator.
    name
        The index's name.
    url
        The index's URL.
    credential_secret
        The secret that holds the index's credential, if it needs one.

    Yields
    ------
    None
        While the index is registered.
    """
    admin.package_indexes.create(name, url, credential_secret=credential_secret)
    try:
        yield
    finally:
        with contextlib.suppress(NotFound):
            admin.package_indexes.delete(name)
