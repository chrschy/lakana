"""Tests for the row-filters resource of the platform API."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_table


def test_create_a_row_filter_and_list_it_for_the_principal(client: TestClient) -> None:
    """A created row filter shows up when listing that principal's filters."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    created = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "id = current_user()",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["expression"] == "id = current_user()"
    assert body["namespace"] == "gold"

    listed = client.get(
        "/api/v1/row-filters", headers=AUTH_HEADER, params={"principal_name": "alice"}
    )
    assert listed.status_code == 200
    assert [f["id"] for f in listed.json()] == [body["id"]]


def test_create_requires_exactly_one_of_role_or_action(client: TestClient) -> None:
    """Passing neither principal nor group is a 422."""
    response = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "TRUE",
        },
    )
    assert response.status_code == 422


def test_create_rejects_an_expression_outside_the_restricted_subset(client: TestClient) -> None:
    """An out-of-grammar expression is a 422, not stored."""
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    response = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "(SELECT 1)",
        },
    )
    assert response.status_code == 422


def test_list_by_table_requires_all_three_fields_together(client: TestClient) -> None:
    """Listing by table needs environment, namespace, and table all given."""
    response = client.get("/api/v1/row-filters", headers=AUTH_HEADER, params={"environment": "dev"})
    assert response.status_code == 422


def test_list_by_table_is_an_exact_match(client: TestClient) -> None:
    """Listing by table finds only the exact environment/namespace/table given."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "TRUE",
        },
    )
    matching = client.get(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        params={"environment": "dev", "namespace": "gold", "table": "customers"},
    )
    assert len(matching.json()) == 1
    different_table = client.get(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        params={"environment": "dev", "namespace": "gold", "table": "orders"},
    )
    assert different_table.json() == []


def test_list_row_filters_for_a_group(client: TestClient) -> None:
    """Listing by group_name shows that group's filters."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "admins"})
    client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "group": "admins",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "TRUE",
        },
    )
    listed = client.get("/api/v1/row-filters", headers=AUTH_HEADER, params={"group_name": "admins"})
    assert len(listed.json()) == 1


def test_revoke_a_row_filter(client: TestClient) -> None:
    """A revoked row filter no longer appears when listing the principal's filters."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    created = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "TRUE",
        },
    )
    row_filter_id = created.json()["id"]

    revoked = client.delete(f"/api/v1/row-filters/{row_filter_id}", headers=AUTH_HEADER)
    assert revoked.status_code == 204

    listed = client.get(
        "/api/v1/row-filters", headers=AUTH_HEADER, params={"principal_name": "alice"}
    )
    assert listed.json() == []
