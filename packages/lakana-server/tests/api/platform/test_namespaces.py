"""Tests for the namespaces platform API."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADER


def _create_env(client: TestClient, storage_root: str, name: str = "dev") -> None:
    """Create an environment, ignoring the response body.

    Parameters
    ----------
    client
        The test client.
    storage_root
        The environment's storage root URI.
    name
        Environment name.
    """
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": name,
            "storage": {"scheme": "file", "root": storage_root, "options": {}},
        },
    )
    assert response.status_code == 201, response.text


def test_create_then_get_round_trips(client: TestClient, storage_root: str) -> None:
    """A created namespace can be fetched back by its dotted path."""
    _create_env(client, storage_root)
    created = client.post(
        "/api/v1/environments/dev/namespaces",
        headers=AUTH_HEADER,
        json={"path": "gold.sales", "properties": {"owner": "analytics"}},
    )
    assert created.status_code == 201, created.text
    fetched = client.get("/api/v1/environments/dev/namespaces/gold.sales", headers=AUTH_HEADER)
    assert fetched.status_code == 200
    assert fetched.json() == {"path": "gold.sales", "properties": {"owner": "analytics"}}


def test_get_unknown_is_404(client: TestClient, storage_root: str) -> None:
    """A missing namespace is a 404."""
    _create_env(client, storage_root)
    response = client.get("/api/v1/environments/dev/namespaces/nope", headers=AUTH_HEADER)
    assert response.status_code == 404


def test_list_returns_every_namespace_in_the_environment(
    client: TestClient, storage_root: str
) -> None:
    """Unfiltered listing, matching the environments router's own carry-forward.

    `create_environment` auto-creates `bronze`/`silver`/`gold`, so the
    listing includes those alongside whatever this test creates. The
    created namespaces are named outside that set (`"widgets"`,
    `"widgets.sales"`, `"raw"`) so each create call actually succeeds
    instead of silently 409-ing against the scaffold.
    """
    _create_env(client, storage_root)
    for path in ("widgets", "widgets.sales", "raw"):
        response = client.post(
            "/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, json={"path": path}
        )
        assert response.status_code == 201, response.text
    response = client.get("/api/v1/environments/dev/namespaces", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert {ns["path"] for ns in response.json()} == {
        "bronze",
        "silver",
        "gold",
        "widgets",
        "widgets.sales",
        "raw",
    }


def test_drop_removes_the_namespace(client: TestClient, storage_root: str) -> None:
    """A dropped namespace is gone from a subsequent get."""
    _create_env(client, storage_root)
    client.post("/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, json={"path": "gold"})
    response = client.delete("/api/v1/environments/dev/namespaces/gold", headers=AUTH_HEADER)
    assert response.status_code == 204
    assert (
        client.get("/api/v1/environments/dev/namespaces/gold", headers=AUTH_HEADER).status_code
        == 404
    )


def test_drop_a_namespace_containing_tables_is_a_conflict(
    client: TestClient, storage_root: str
) -> None:
    """The ConflictError from CatalogService surfaces as 409, not 500.

    The platform API has no way to create a table; the Iceberg router does.
    """
    _create_env(client, storage_root)
    client.post("/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, json={"path": "gold"})
    client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={
            "name": "sales",
            "schema": {
                "type": "struct",
                "schema-id": 0,
                "fields": [{"id": 1, "name": "id", "type": "long", "required": True}],
            },
            "properties": {},
        },
    )
    response = client.delete("/api/v1/environments/dev/namespaces/gold", headers=AUTH_HEADER)
    assert response.status_code == 409
