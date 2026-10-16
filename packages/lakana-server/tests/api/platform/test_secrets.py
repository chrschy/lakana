"""Tests for the secrets resource of the platform API."""

import logging

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from tests.conftest import AUTH_HEADER


def _create_dev_environment(client: TestClient, storage_root: str) -> None:
    """Create a bare `dev` environment for a test to attach secrets to.

    Parameters
    ----------
    client
        The test client.
    storage_root
        The environment's storage root URI.
    """
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": storage_root},
        },
    )


def test_create_a_secret_and_list_it(client: TestClient, storage_root: str) -> None:
    """A created secret shows up when listing its environment's secrets, with no value field."""
    _create_dev_environment(client, storage_root)

    created = client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"access_key_id": "AKIA123"}},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "s3-creds"
    assert "values" not in body
    assert "ciphertext" not in body

    listed = client.get("/api/v1/environments/dev/secrets", headers=AUTH_HEADER)
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()] == ["s3-creds"]
    assert "values" not in listed.json()[0]
    assert listed.json()[0]["format_version"] == created.json()["format_version"]
    assert listed.json()[0]["key_id"].startswith("env:")


def test_create_raises_not_found_for_an_unknown_environment(client: TestClient) -> None:
    """Creating a secret in an environment that doesn't exist is a 404."""
    response = client.post(
        "/api/v1/environments/nope/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"k": "v"}},
    )
    assert response.status_code == 404


def test_rotate_a_secret(client: TestClient, storage_root: str) -> None:
    """PATCH re-encrypts a secret's value under the same name."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"k": "old"}},
    )

    rotated = client.patch(
        "/api/v1/environments/dev/secrets/s3-creds",
        headers=AUTH_HEADER,
        json={"values": {"k": "new"}},
    )
    assert rotated.status_code == 200, rotated.text
    assert "values" not in rotated.json()


def test_rotate_raises_not_found_for_an_unknown_secret_name(
    client: TestClient, storage_root: str
) -> None:
    """Rotating a secret name that doesn't exist in that environment is a 404."""
    _create_dev_environment(client, storage_root)

    response = client.patch(
        "/api/v1/environments/dev/secrets/nope", headers=AUTH_HEADER, json={"values": {"k": "v"}}
    )
    assert response.status_code == 404


def test_revoke_a_secret(client: TestClient, storage_root: str) -> None:
    """DELETE revokes a secret; it no longer appears when listing."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"k": "v"}},
    )

    revoked = client.delete("/api/v1/environments/dev/secrets/s3-creds", headers=AUTH_HEADER)
    assert revoked.status_code == 204

    listed = client.get("/api/v1/environments/dev/secrets", headers=AUTH_HEADER)
    assert listed.json() == []


def test_revoke_raises_not_found_for_an_unknown_secret_name(
    client: TestClient, storage_root: str
) -> None:
    """Revoking a secret name that doesn't exist in that environment is a 404."""
    _create_dev_environment(client, storage_root)

    response = client.delete("/api/v1/environments/dev/secrets/nope", headers=AUTH_HEADER)
    assert response.status_code == 404


def test_rotate_raises_not_found_when_other_secrets_exist(
    client: TestClient, storage_root: str
) -> None:
    """Looking up an unknown name still 404s when it must skip past other secrets first."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "a", "values": {"k": "v"}},
    )
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "b", "values": {"k": "v"}},
    )

    response = client.patch(
        "/api/v1/environments/dev/secrets/nope", headers=AUTH_HEADER, json={"values": {"k": "v"}}
    )
    assert response.status_code == 404


_SENTINEL = "hunter2-sentinel-value"

_BAD_VALUES = [
    pytest.param({"k": 7, "other": _SENTINEL}, id="wrong-type-of-a-value"),
    pytest.param({_SENTINEL: 7}, id="secret-typed-into-a-key"),
    pytest.param({"k": [_SENTINEL]}, id="list-instead-of-string"),
    pytest.param({"k": {"inner": _SENTINEL}}, id="object-instead-of-string"),
    pytest.param([_SENTINEL], id="values-is-a-list"),
    pytest.param(_SENTINEL, id="values-is-a-string"),
]


def _assert_no_sentinel(response: Response) -> None:
    """Assert a 422 whose whole body carries none of the submitted secret text.

    Parameters
    ----------
    response
        The response to check.
    """
    assert response.status_code == 422, response.text
    assert _SENTINEL not in response.text


@pytest.mark.parametrize("values", _BAD_VALUES)
def test_a_rejected_create_does_not_echo_what_was_submitted(
    client: TestClient, storage_root: str, values: object
) -> None:
    """A create that fails validation answers 422 without any submitted value."""
    _create_dev_environment(client, storage_root)
    _assert_no_sentinel(
        client.post(
            "/api/v1/environments/dev/secrets",
            headers=AUTH_HEADER,
            json={"name": "s", "values": values},
        )
    )


@pytest.mark.parametrize("name", [[_SENTINEL], {_SENTINEL: 1}, None])
def test_a_rejected_secret_name_does_not_echo_the_values_beside_it(
    client: TestClient, storage_root: str, name: object
) -> None:
    """A create whose name is wrong answers 422 without the valid secret values sent with it."""
    _create_dev_environment(client, storage_root)
    _assert_no_sentinel(
        client.post(
            "/api/v1/environments/dev/secrets",
            headers=AUTH_HEADER,
            json={"name": name, "values": {"k": _SENTINEL}},
        )
    )


@pytest.mark.parametrize("values", _BAD_VALUES)
def test_a_rejected_rotate_does_not_echo_what_was_submitted(
    client: TestClient, storage_root: str, values: object
) -> None:
    """A rotate that fails validation answers 422 without any submitted value."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s", "values": {"k": "v"}},
    )
    _assert_no_sentinel(
        client.patch(
            "/api/v1/environments/dev/secrets/s", headers=AUTH_HEADER, json={"values": values}
        )
    )


def test_a_rejected_secret_request_writes_nothing_of_it_to_the_log(
    client: TestClient, storage_root: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The application's own log carries none of a rejected secret body."""
    _create_dev_environment(client, storage_root)
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/api/v1/environments/dev/secrets",
            headers=AUTH_HEADER,
            json={"name": "s", "values": {_SENTINEL: 7}},
        )
    assert _SENTINEL not in caplog.text


def test_a_malformed_json_body_does_not_echo_what_was_submitted(
    client: TestClient, storage_root: str
) -> None:
    """A body that is not JSON at all answers 422 without the text that was sent."""
    _create_dev_environment(client, storage_root)
    _assert_no_sentinel(
        client.post(
            "/api/v1/environments/dev/secrets",
            headers={**AUTH_HEADER, "content-type": "application/json"},
            content=f'{{"name": "s", "values": {{"k": {_SENTINEL}}}}}'.encode(),
        )
    )


_BAD_OPTIONS = [
    pytest.param({_SENTINEL: 1}, id="secret-typed-into-a-key"),
    pytest.param({"k": [_SENTINEL]}, id="list-instead-of-string"),
    pytest.param({"k": {"inner": _SENTINEL}}, id="object-instead-of-string"),
    pytest.param([_SENTINEL], id="options-is-a-list"),
    pytest.param(_SENTINEL, id="options-is-a-string"),
]


@pytest.mark.parametrize("options", _BAD_OPTIONS)
def test_a_rejected_environment_create_does_not_echo_the_storage_options(
    client: TestClient, storage_root: str, options: object
) -> None:
    """Storage options can carry inline S3 credentials, so a refusal names no key and no value."""
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": storage_root, "options": options},
        },
    )
    _assert_no_sentinel(response)
    assert "options" in response.text


@pytest.mark.parametrize("options", _BAD_OPTIONS)
def test_a_rejected_storage_update_does_not_echo_the_options(
    client: TestClient, storage_root: str, options: object
) -> None:
    """A PATCH of the storage options answers 422 without any submitted key or value."""
    _create_dev_environment(client, storage_root)
    _assert_no_sentinel(
        client.patch(
            "/api/v1/environments/dev/storage", headers=AUTH_HEADER, json={"options": options}
        )
    )


def test_a_storage_update_without_options_is_still_accepted(
    client: TestClient, storage_root: str
) -> None:
    """`options` stays optional on a PATCH: leaving it out, or null, changes nothing."""
    _create_dev_environment(client, storage_root)
    for body in ({}, {"options": None}, {"options": {"endpoint": "http://s3.corp"}}):
        response = client.patch("/api/v1/environments/dev/storage", headers=AUTH_HEADER, json=body)
        assert response.status_code == 200, response.text
