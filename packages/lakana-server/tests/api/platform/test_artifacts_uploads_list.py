"""Listing one's own open uploads over REST."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid

from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_namespace

_LIST = "/api/v1/environments/dev/artifacts/uploads"
_BASE = "/api/v1/environments/dev/namespaces/models/artifacts"
_DOC = {
    "format": 1,
    "is_directory": False,
    "files": [{"path": "m", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}],
}


def _begin(client: TestClient, name: str, headers: dict[str, str] | None = None) -> str:
    """Begin an upload of `models.<name>`.

    Parameters
    ----------
    client
        The test client.
    name
        The artifact's name.
    headers
        The caller's headers; the admin's by default.

    Returns
    -------
    str
        The upload id.
    """
    begun = client.post(f"{_BASE}/{name}/uploads", headers=headers or AUTH_HEADER, json=_DOC)
    assert begun.status_code == 201, begun.text
    return begun.json()["upload_id"]


def _user(client: TestClient, name: str, role: str) -> dict[str, str]:
    """Create a principal with a role on `dev`.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    role
        The role to grant on the environment.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": name, "role": role, "kind": "ENVIRONMENT", "environment": "dev"},
    )
    assert granted.status_code == 201, granted.text
    token = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_a_caller_lists_their_open_uploads(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    first = _begin(client, "churn")
    second = _begin(client, "other")
    listed = client.get(_LIST, headers=AUTH_HEADER)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["next_cursor"] is None
    assert [(i["upload_id"], i["artifact"]) for i in body["items"]] == [
        (second, "models.other"),
        (first, "models.churn"),
    ]
    assert set(body["items"][0]) == {"upload_id", "artifact", "created_at", "expires_at"}


def test_nobody_else_sees_an_upload_and_nothing_says_that_one_exists(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    other = _user(client, "scientist", "DATA_SCIENTIST")
    _begin(client, "churn")
    mine = _begin(client, "churn", other)
    theirs = client.get(_LIST, headers=other).json()
    assert [(i["upload_id"], i["artifact"]) for i in theirs["items"]] == [(mine, "models.churn")]
    quiet = _user(client, "reader", "VIEWER")
    assert client.get(_LIST, headers=quiet).json() == {"items": [], "next_cursor": None}


def test_a_name_narrows_the_list(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    wanted = _begin(client, "churn")
    _begin(client, "other")
    found = client.get(_LIST, params={"name": "models.churn"}, headers=AUTH_HEADER).json()
    assert [i["upload_id"] for i in found["items"]] == [wanted]
    bad = client.get(_LIST, params={"name": "churn"}, headers=AUTH_HEADER)
    assert bad.status_code == 422


def test_the_list_pages_with_limit_and_cursor(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    made = [_begin(client, f"m{i}") for i in range(5)]
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = client.get(_LIST, params=params, headers=AUTH_HEADER).json()
        assert len(page["items"]) <= 2
        seen += [i["upload_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == list(reversed(made))


def test_a_limit_out_of_range_and_a_cursor_nobody_issued_are_refused(client: TestClient) -> None:
    create_environment(client)
    naive = base64.urlsafe_b64encode(
        json.dumps({"c": "2026-10-07T12:00:00", "i": str(uuid.uuid4())}).encode()
    ).decode()

    def cursor(content: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(content).encode()).decode().rstrip("=")

    zero = "00000000-0000-0000-0000-000000000000"
    for params in (
        {"limit": 0},
        {"limit": 501},
        {"cursor": "not-a-cursor"},
        {"cursor": "e30"},
        {"cursor": naive},
        {"cursor": cursor({"c": "2026-01-01T00:00:00+00:00", "i": 1})},  # a number as the id
        {"cursor": cursor({"c": "2026-01-01T00:00:00+00:00", "i": ["a"]})},
        {"cursor": cursor({"c": 5, "i": zero})},
        {"cursor": cursor([1, 2])},  # not an object
        {"cursor": cursor({"c": "0001-01-01T00:00:00+23:59", "i": zero})},  # overflows in UTC
        {"cursor": cursor({"c": "9999-12-31T23:59:59-23:59", "i": zero})},
    ):
        refused = client.get(_LIST, params=params, headers=AUTH_HEADER)
        assert refused.status_code == 422, params
        if "cursor" in params:
            detail = refused.json()["detail"]
            assert "cursor is not valid" in detail and "without" in detail, detail
            assert params["cursor"] not in detail


def test_the_environment_must_exist_and_be_readable(client: TestClient) -> None:
    create_environment(client)
    assert client.get(_LIST.replace("dev", "nowhere"), headers=AUTH_HEADER).status_code == 404
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "nobody", "type": "USER"})
    token = client.post(
        "/api/v1/principals/nobody/tokens", headers=AUTH_HEADER, json={"name": "nobody"}
    ).json()["token"]
    refused = client.get(_LIST, headers={"Authorization": f"Bearer {token}"})
    assert refused.status_code == 403
    assert client.get(_LIST).status_code == 401


def test_an_artifact_called_uploads_is_still_an_artifact(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    _begin(client, "uploads")
    listed = client.get(_LIST, headers=AUTH_HEADER).json()["items"]
    assert [i["artifact"] for i in listed] == ["models.uploads"]
    assert client.get(f"{_BASE}/uploads", headers=AUTH_HEADER).json()["name"] == "uploads"
