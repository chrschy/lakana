"""Tests for the grants resource of the platform API."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, SecurableKind
from lakana_core.models import Grant, Principal
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_namespace, create_table


def test_grant_a_role_and_list_it_for_the_principal(client: TestClient) -> None:
    """A granted role shows up when listing that principal's grants."""
    create_environment(client)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    created = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "alice", "role": "ANALYST", "kind": "ENVIRONMENT", "environment": "dev"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["role"] == "ANALYST"
    assert body["environment"] == "dev"

    listed = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"principal_name": "alice"})
    assert listed.status_code == 200
    assert [g["id"] for g in listed.json()] == [body["id"]]


def test_grant_requires_exactly_one_of_role_or_action(client: TestClient) -> None:
    """Passing neither role nor action is a 422."""
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    response = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "alice", "kind": "INSTANCE"},
    )
    assert response.status_code == 422


def test_grant_a_namespace_scoped_role_round_trips_the_dotted_path(client: TestClient) -> None:
    """A grant's namespace crosses the API as a dotted string, not the internal join form."""
    create_environment(client)
    create_namespace(client, "dev", "gold.sales")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    created = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "role": "VIEWER",
            "kind": "NAMESPACE",
            "environment": "dev",
            "namespace": "gold.sales",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["namespace"] == "gold.sales"


def test_grant_on_a_missing_namespace_is_404(client: TestClient) -> None:
    """Granting a NAMESPACE-kind grant on a namespace that doesn't exist is a 404."""
    create_environment(client)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    response = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "role": "VIEWER",
            "kind": "NAMESPACE",
            "environment": "dev",
            "namespace": "gold.sales",
        },
    )
    assert response.status_code == 404, response.text


def test_list_for_securable_infers_kind_from_the_given_fields(client: TestClient) -> None:
    """Listing by environment+namespace+table finds only an exact TABLE-kind match."""
    create_environment(client)
    create_table(client, "dev", "bronze", "orders")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "action": "READ_TABLE",
            "kind": "TABLE",
            "environment": "dev",
            "namespace": "bronze",
            "table": "orders",
        },
    )
    at_table = client.get(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        params={"environment": "dev", "namespace": "bronze", "table": "orders"},
    )
    assert len(at_table.json()) == 1
    at_namespace = client.get(
        "/api/v1/grants", headers=AUTH_HEADER, params={"environment": "dev", "namespace": "bronze"}
    )
    assert at_namespace.json() == []


def test_revoke_a_grant(client: TestClient) -> None:
    """A revoked grant no longer appears when listing the principal's grants."""
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    created = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "alice", "role": "VIEWER", "kind": "INSTANCE"},
    )
    grant_id = created.json()["id"]

    revoked = client.delete(f"/api/v1/grants/{grant_id}", headers=AUTH_HEADER)
    assert revoked.status_code == 204

    listed = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"principal_name": "alice"})
    assert listed.json() == []


def test_revoking_the_last_admin_grant_is_409(client: TestClient) -> None:
    """Revoking the bootstrap admin's own instance-admin grant is refused."""
    listed = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"principal_name": "admin"})
    admin_grant_id = next(g["id"] for g in listed.json() if g["role"] == "ADMIN")

    response = client.delete(f"/api/v1/grants/{admin_grant_id}", headers=AUTH_HEADER)
    assert response.status_code == 409


def test_grant_a_role_and_list_it_for_the_group(client: TestClient) -> None:
    """A granted role shows up when listing that group's grants."""
    client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "engineers"})
    created = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"group": "engineers", "role": "VIEWER", "kind": "INSTANCE"},
    )
    assert created.status_code == 201, created.text

    listed = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"group_name": "engineers"})
    assert listed.status_code == 200
    assert [g["id"] for g in listed.json()] == [created.json()["id"]]


def test_list_for_securable_infers_environment_kind_when_only_environment_given(
    client: TestClient,
) -> None:
    """Listing by environment alone finds only an exact ENVIRONMENT-kind match."""
    create_environment(client)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    created = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "alice", "role": "ANALYST", "kind": "ENVIRONMENT", "environment": "dev"},
    )
    assert created.status_code == 201, created.text

    listed = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"environment": "dev"})
    assert [g["id"] for g in listed.json()] == [created.json()["id"]]


def test_listing_without_a_filter_includes_the_instance_grants(client: TestClient) -> None:
    """Listing with no filters at all lists every grant, the bootstrap admin's among them."""
    listed = client.get("/api/v1/grants", headers=AUTH_HEADER)
    assert listed.status_code == 200
    assert any(g["role"] == "ADMIN" for g in listed.json())


def test_dropping_and_recreating_an_environment_revokes_its_stale_grant(client: TestClient) -> None:
    """A grant on a dropped-and-recreated environment must not still authorize.

    `environments.id` is a `uuid.uuid4()`, so a re-created `prod` can never
    reuse the old row's id — `Grant.environment_id`'s `ON DELETE SET NULL`
    alone already makes bob's stale grant fail `authorize()` closed here,
    independent of `revoke_attached`. What this test actually pins is that
    the API surface for the stale-grant exploit stays closed end-to-end; the
    `revoked_at`/`revoked_by`/audit-trail bookkeeping that only
    `revoke_attached` provides (so admin listings by name don't keep
    showing a dead grant as active) is instead pinned at the core level by
    `test_delete_environment_revokes_every_attached_row`, and by the
    namespace case just below — a namespace has no id binding on `Grant` at
    all, so there `revoke_attached` is the *only* thing that closes the
    exploit.
    """
    create_environment(client, "prod")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"})
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "bob", "role": "VIEWER", "kind": "ENVIRONMENT", "environment": "prod"},
    )
    issued = client.post(
        "/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "bob-pat"}
    )
    assert issued.status_code == 201, issued.text
    bob_headers = {"Authorization": f"Bearer {issued.json()['token']}"}
    assert client.get("/api/v1/environments/prod", headers=bob_headers).status_code == 200

    deleted = client.delete("/api/v1/environments/prod", headers=AUTH_HEADER)
    assert deleted.status_code == 204, deleted.text
    create_environment(client, "prod")

    response = client.get("/api/v1/environments/prod", headers=bob_headers)
    assert response.status_code == 403


def test_dropping_and_recreating_a_namespace_revokes_its_stale_grant(client: TestClient) -> None:
    """Revocation, not id binding, is what closes the exploit for a namespace.

    Unlike `Grant.environment_id`/`table_id`, a namespace has no id column
    on `Grant` at all — a NAMESPACE-kind grant matches on `environment_id`
    plus the literal namespace string, and that string is identical before
    and after the drop-and-recreate below. With `revoke_attached`'s call in
    `drop_namespace` removed, bob's grant on `reports` would still match the
    re-created `reports` exactly as it matched the old one, and this test
    fails (verified locally by commenting out that one call site and
    re-running: `GET .../reports` as bob stayed 200 instead of 403). This is
    the one stale-grant regression where revocation is the sole defence.
    """
    create_environment(client, "dev")
    create_namespace(client, "dev", "reports")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"})
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "bob",
            "role": "VIEWER",
            "kind": "NAMESPACE",
            "environment": "dev",
            "namespace": "reports",
        },
    )
    issued = client.post(
        "/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "bob-pat"}
    )
    assert issued.status_code == 201, issued.text
    bob_headers = {"Authorization": f"Bearer {issued.json()['token']}"}
    assert (
        client.get("/api/v1/environments/dev/namespaces/reports", headers=bob_headers).status_code
        == 200
    )

    deleted = client.delete("/api/v1/environments/dev/namespaces/reports", headers=AUTH_HEADER)
    assert deleted.status_code == 204, deleted.text
    create_namespace(client, "dev", "reports")

    response = client.get("/api/v1/environments/dev/namespaces/reports", headers=bob_headers)
    assert response.status_code == 403


@pytest.mark.parametrize("action", ["MANAGE_GRANT", "MANAGE_GROUP", "MANAGE_TOKEN"])
def test_a_single_action_grant_of_the_three_is_a_422(client: TestClient, action: str) -> None:
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"})
    refused = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "bob", "action": action, "kind": "INSTANCE"},
    )
    assert refused.status_code == 422, refused.text
    assert (
        f"{action} comes only with the ADMIN role on the instance; grant that role instead, "
        "or nothing." in refused.text
    )


def test_a_listed_single_action_grant_of_the_three_carries_a_note(client: TestClient) -> None:
    """The note names the action; a role grant has none; the stale grant can be revoked."""
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"})
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "bob", "role": "VIEWER", "kind": "INSTANCE"},
    )
    with Session(client.app.state.engine) as session:
        bob = session.execute(select(Principal).where(Principal.name == "bob")).scalar_one()
        session.add(
            Grant(
                id=uuid.uuid4(),
                principal_id=bob.id,
                action=Action.MANAGE_TOKEN,
                kind=SecurableKind.INSTANCE,
                granted_by=bob.id,
                granted_at=datetime.now(UTC),
            )
        )
        session.commit()
    listed = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"principal_name": "bob"})
    assert listed.status_code == 200, listed.text
    notes = {(row["role"], row["action"]): row["note"] for row in listed.json()}
    assert notes == {
        ("VIEWER", None): None,
        (None, "MANAGE_TOKEN"): "no effect: MANAGE_TOKEN needs the ADMIN role",
    }
    stale = next(row["id"] for row in listed.json() if row["action"] == "MANAGE_TOKEN")
    assert client.delete(f"/api/v1/grants/{stale}", headers=AUTH_HEADER).status_code == 204
