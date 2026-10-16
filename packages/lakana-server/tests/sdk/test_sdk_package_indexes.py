"""`client.package_indexes` against the in-process server."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import lakana
from lakana.errors import Conflict, NotFound, PermissionDenied, ValidationError


def test_package_index_lifecycle(lakana_client: lakana.Client) -> None:
    index = lakana_client.package_indexes.create(
        "team", "https://pkgs.example.org/simple/", credential_secret="team-index"
    )
    assert (index.name, index.url, index.credential_secret) == (
        "team",
        "https://pkgs.example.org/simple",
        "team-index",
    )
    assert [i.name for i in lakana_client.package_indexes.list()] == ["team"]
    assert lakana_client.package_indexes.get("team").created_at.tzinfo is not None
    updated = lakana_client.package_indexes.update("team", credential_secret=None)
    assert updated.credential_secret is None
    with pytest.raises(Conflict):
        lakana_client.package_indexes.create("team", "https://other.example.org/simple")
    lakana_client.package_indexes.delete("team")
    with pytest.raises(NotFound):
        lakana_client.package_indexes.get("team")


def test_update_refuses_unknown_fields_client_side(lakana_client: lakana.Client) -> None:
    # `name` is the positional parameter, so Python itself refuses it; a field that is neither
    # updatable nor a parameter reaches the SDK's own check, before any request.
    with pytest.raises(TypeError, match=r"update\(\) got unexpected field\(s\): kind"):
        lakana_client.package_indexes.update("team", kind="x")


def test_an_invalid_url_is_a_validation_error(lakana_client: lakana.Client) -> None:
    with pytest.raises(ValidationError, match="must use https"):
        lakana_client.package_indexes.create("team", "http://pkgs.example.org/simple")


def test_a_non_admin_gets_permission_denied(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    lakana_client.package_indexes.create("team", "https://pkgs.example.org/simple")
    lakana_client.principals.create("bob", "USER")
    token = lakana_client.tokens.issue("bob", "laptop").token
    with lakana.Client(url="http://testserver", token=token, http_client=client) as bob:
        with pytest.raises(PermissionDenied):
            bob.package_indexes.create("x", "https://x.example.org/simple")
        with pytest.raises(PermissionDenied):
            bob.package_indexes.update("team", credential_secret="s")
        with pytest.raises(PermissionDenied):
            bob.package_indexes.delete("team")
        with pytest.raises(PermissionDenied):
            bob.package_indexes.list()
