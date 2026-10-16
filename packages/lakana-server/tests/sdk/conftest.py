"""Fixtures for the SDK end-to-end tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import lakana


@pytest.fixture
def lakana_client(client: TestClient) -> Iterator[lakana.Client]:
    """Provide a `lakana.Client` wired to the in-process test server.

    Parameters
    ----------
    client
        The Starlette `TestClient` fixture (`tests.conftest`), already
        running the app's lifespan so the bootstrap principal exists. Passed
        as `http_client` so the SDK sends requests through it directly —
        no real network I/O, and `lakana.Client.close()` never closes it,
        since ownership stays with the `client` fixture.

    Yields
    ------
    lakana.Client
        An SDK client authenticated as the bootstrap PAT.
    """
    with lakana.Client(
        url="http://testserver", token="lakana_pat_testkey_testsecret", http_client=client
    ) as sdk_client:
        yield sdk_client
