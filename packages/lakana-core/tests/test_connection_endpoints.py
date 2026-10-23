"""`endpoints`: the exact host:port pairs a task of each kind may reach (pinned)."""

from __future__ import annotations

from typing import Any

import pytest

from lakana_core.connections.endpoints import (
    MOUNT_KINDS,
    PROXY_KINDS,
    RELAY_KINDS,
    Endpoint,
    endpoints,
)
from lakana_core.connections.kinds import ConnectionKind as K
from lakana_core.connections.kinds import validate_connection

PINNED: list[tuple[K, dict[str, Any], dict[str, Any], tuple[Endpoint, ...]]] = [
    (
        K.POSTGRESQL,
        {"host": "db.example.com", "port": 5432, "database": "x"},
        {"user": "r"},
        (Endpoint("db.example.com", 5432, "service"),),
    ),
    (
        K.MARIADB,
        {"host": "10.0.0.5", "port": 3306, "database": "x"},
        {"user": "r"},
        (Endpoint("10.0.0.5", 3306, "service"),),
    ),
    (K.SQLITE_FILE, {"root": "r", "path": "a.db"}, {}, ()),
    (
        K.HTTP,
        {"base_url": "https://api.example.com:8443/v1"},
        {"auth": "none"},
        (Endpoint("api.example.com", 8443, "service"),),
    ),
    (
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "oauth2", "client_id": "c", "token_url": "https://login.example.com/oauth/token"},
        (Endpoint("api.example.com", 443, "service"), Endpoint("login.example.com", 443, "token")),
    ),
    (
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "oauth2", "client_id": "c", "token_url": "https://api.example.com/token"},
        (Endpoint("api.example.com", 443, "service"),),
    ),
    (
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "oauth2", "client_id": "c", "token_url": "https://api.example.com:443/token"},
        (Endpoint("api.example.com", 443, "service"),),
    ),
    (
        K.S3,
        {
            "bucket": "b",
            "region": "eu-central-1",
            "endpoint": "https://s3.eu-central-1.amazonaws.com",
        },
        {"access_key_id": "a"},
        (Endpoint("s3.eu-central-1.amazonaws.com", 443, "service"),),
    ),
    (
        K.GCS,
        {"bucket": "b"},
        {},
        (
            Endpoint("storage.googleapis.com", 443, "service"),
            Endpoint("oauth2.googleapis.com", 443, "token"),
        ),
    ),
    (
        K.AZURE,
        {"account": "acme01", "container": "c"},
        {},
        (Endpoint("acme01.blob.core.windows.net", 443, "service"),),
    ),
    (K.DIRECTORY, {"root": "r", "path": "."}, {}, ()),
    (
        K.TCP,
        {"host": "erp.example.com", "port": 1433},
        {},
        (Endpoint("erp.example.com", 1433, "service"),),
    ),
]


@pytest.mark.parametrize(("kind", "address", "settings", "expected"), PINNED)
def test_endpoints_per_kind(
    kind: K, address: dict[str, Any], settings: dict[str, Any], expected: tuple[Endpoint, ...]
) -> None:
    """The worker grants exactly these, and the fetch route returns exactly these."""
    assert endpoints(kind, address, settings) == expected


def test_every_kind_has_exactly_one_route() -> None:
    """Proxy, relay or mount: each kind is in one set, so no kind falls through the worker."""
    assert frozenset(K) == PROXY_KINDS | RELAY_KINDS | MOUNT_KINDS
    assert not (PROXY_KINDS & RELAY_KINDS or PROXY_KINDS & MOUNT_KINDS or RELAY_KINDS & MOUNT_KINDS)


def test_a_default_s3_endpoint_is_an_endpoint() -> None:
    """The endpoint `validate_connection` fills in is the one a task reaches."""
    address, settings = validate_connection(
        K.S3, {"bucket": "raw-data", "region": "us-east-2"}, {"access_key_id": "a"}
    )
    assert endpoints(K.S3, address, settings) == (
        Endpoint("s3.us-east-2.amazonaws.com", 443, "service"),
    )
