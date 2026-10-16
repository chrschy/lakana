"""The package-indexes resource of the platform API."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, AuditEventType
from tests.api.platform.test_artifacts_delete import _task_headers
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_TEAM = {
    "name": "team",
    "url": "https://pkgs.example.org/simple",
    "credential_secret": "team-index",
}
_BASE = "/api/v1/package-indexes"


def _token_for(client: TestClient, name: str) -> dict[str, str]:
    """Create a service account with a PAT and return its auth header.

    Parameters
    ----------
    client
        The test client.
    name
        Principal name.

    Returns
    -------
    dict of str to str
        An Authorization header.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "SERVICE_ACCOUNT"}
    )
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def test_register_list_show_update_delete(client: TestClient) -> None:
    created = client.post(
        _BASE, headers=AUTH_HEADER, json={**_TEAM, "url": "https://pkgs.example.org/simple/"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["url"] == "https://pkgs.example.org/simple"
    assert set(body) == {
        "id",
        "name",
        "url",
        "credential_secret",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert [row["name"] for row in client.get(_BASE, headers=AUTH_HEADER).json()] == ["team"]
    shown = client.get(f"{_BASE}/team", headers=AUTH_HEADER)
    assert shown.json()["credential_secret"] == "team-index"
    patched = client.patch(f"{_BASE}/team", headers=AUTH_HEADER, json={"credential_secret": None})
    assert patched.status_code == 200, patched.text
    assert patched.json()["credential_secret"] is None
    assert client.delete(f"{_BASE}/team", headers=AUTH_HEADER).status_code == 204
    assert client.get(f"{_BASE}/team", headers=AUTH_HEADER).status_code == 404


def test_a_url_with_a_credential_is_a_422_that_does_not_repeat_it(client: TestClient) -> None:
    response = client.post(
        _BASE,
        headers=AUTH_HEADER,
        json={"name": "team", "url": "https://ci:hunter2@pkgs.example.org/simple"},
    )
    assert response.status_code == 422
    assert "Store them in a secret" in response.text
    assert "hunter2" not in response.text


def test_duplicates_fixed_fields_and_empty_patches(client: TestClient) -> None:
    assert client.post(_BASE, headers=AUTH_HEADER, json=_TEAM).status_code == 201
    assert client.post(_BASE, headers=AUTH_HEADER, json=_TEAM).status_code == 409
    other = client.post(_BASE, headers=AUTH_HEADER, json={"name": "other", "url": _TEAM["url"]})
    assert other.status_code == 409
    assert "'team' already has this URL" in other.text
    for patch in ({"name": "x"}, {}, {"url": None}):
        refused = client.patch(f"{_BASE}/team", headers=AUTH_HEADER, json=patch)
        assert refused.status_code == 422, (patch, refused.text)
    unknown = client.post(
        _BASE, headers=AUTH_HEADER, json={"name": "x", "url": "https://x.example/s", "kind": "y"}
    )
    assert unknown.status_code == 422


def test_only_an_administrator_changes_the_registry(client: TestClient) -> None:
    client.post(_BASE, headers=AUTH_HEADER, json=_TEAM)
    header = _token_for(client, "nobody")
    calls = (
        client.post(_BASE, headers=header, json={"name": "x", "url": "https://x.example/s"}),
        client.patch(f"{_BASE}/team", headers=header, json={"credential_secret": None}),
        client.delete(f"{_BASE}/team", headers=header),
        client.get(_BASE, headers=header),
        client.get(f"{_BASE}/team", headers=header),
        client.get(f"{_BASE}/missing", headers=header),
    )
    assert [call.status_code for call in calls] == [403] * 6
    assert client.get(f"{_BASE}/team", headers=AUTH_HEADER).status_code == 200


def test_a_reader_of_one_environment_lists_and_shows_but_does_not_change(
    client: TestClient,
) -> None:
    """A project author must see which indexes exist and their URLs."""
    client.post(_BASE, headers=AUTH_HEADER, json=_TEAM)
    create_environment(client, "dev")
    header = _token_for(client, "author")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "author",
            "action": "READ_ENVIRONMENT",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert granted.status_code in (200, 201), granted.text
    assert [row["url"] for row in client.get(_BASE, headers=header).json()] == [_TEAM["url"]]
    assert client.get(f"{_BASE}/team", headers=header).status_code == 200
    assert client.get(f"{_BASE}/nope", headers=header).status_code == 404
    assert client.delete(f"{_BASE}/team", headers=header).status_code == 403


def test_every_route_needs_a_credential(client: TestClient) -> None:
    calls = (
        client.post(_BASE, json=_TEAM),
        client.get(_BASE),
        client.get(f"{_BASE}/team"),
        client.patch(f"{_BASE}/team", json={"credential_secret": None}),
        client.delete(f"{_BASE}/team"),
    )
    assert [call.status_code for call in calls] == [401] * 5


def test_a_task_token_is_refused_on_every_route(client: TestClient) -> None:
    """A task has no use for the registry: not even a token whose principal may read."""
    client.post(_BASE, headers=AUTH_HEADER, json=_TEAM)
    create_environment(client, "dev")
    for kind, header in _task_headers(client).items():
        calls = {
            "create": client.post(
                _BASE, headers=header, json={"name": "x", "url": "https://x.example/s"}
            ),
            "list": client.get(_BASE, headers=header),
            "show": client.get(f"{_BASE}/team", headers=header),
            "show missing": client.get(f"{_BASE}/missing", headers=header),
            "update": client.patch(
                f"{_BASE}/team", headers=header, json={"credential_secret": None}
            ),
            "delete": client.delete(f"{_BASE}/team", headers=header),
        }
        for route, response in calls.items():
            assert response.status_code == 403, (kind, route, response.text)
    assert [row["name"] for row in client.get(_BASE, headers=AUTH_HEADER).json()] == ["team"]


def test_a_nul_in_the_name_is_a_422_on_every_route_with_a_name(client: TestClient) -> None:
    client.post(_BASE, headers=AUTH_HEADER, json=_TEAM)
    calls = (
        client.get(f"{_BASE}/te%00am", headers=AUTH_HEADER),
        client.patch(f"{_BASE}/te%00am", headers=AUTH_HEADER, json={"credential_secret": None}),
        client.delete(f"{_BASE}/te%00am", headers=AUTH_HEADER),
    )
    assert [call.status_code for call in calls] == [422] * 3
    assert all("NUL" in call.text for call in calls)
    # A caller who may not use the route learns nothing about the name.
    create_environment(client, "dev")
    nobody = _token_for(client, "nobody")
    assert client.get(f"{_BASE}/te%00am", headers=nobody).status_code == 403
    for headers in _task_headers(client).values():
        assert client.get(f"{_BASE}/te%00am", headers=headers).status_code == 403


def test_a_task_token_refusal_is_audited_and_the_same_principals_pat_still_lists(
    client: TestClient,
) -> None:
    client.post(_BASE, headers=AUTH_HEADER, json=_TEAM)
    create_environment(client, "dev")
    tokens = _task_headers(client)
    assert client.get(_BASE, headers=tokens["unbound"]).status_code == 403
    with Session(client.app.state.engine) as session:
        details = [
            row.detail
            for row in session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED
                )
            ).scalars()
        ]
    assert any("MANAGE_PACKAGE_INDEX" in str(detail) for detail in details), details
    # `etl`, who owns the task tokens, gets a personal access token and lists as before.
    issued = client.post(
        "/api/v1/principals/etl/tokens", headers=AUTH_HEADER, json={"name": "personal"}
    )
    pat = {"Authorization": f"Bearer {issued.json()['token']}"}
    listed = client.get(_BASE, headers=pat)
    assert [row["name"] for row in listed.json()] == ["team"]
