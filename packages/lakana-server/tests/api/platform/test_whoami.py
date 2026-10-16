"""Tests for `GET /api/v1/whoami`."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADER


def test_whoami_describes_a_pat_caller(client: TestClient) -> None:
    """A PAT caller gets its principal's name and type, and no task-token scope."""
    response = client.get("/api/v1/whoami", headers=AUTH_HEADER)

    assert response.status_code == 200, response.text
    assert response.json() == {"name": "admin", "type": "USER", "task_token": None}


def test_whoami_describes_a_task_token_callers_scope(client: TestClient) -> None:
    """A task-token caller also gets the token's environment and asset scope."""
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.sales"],
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text

    response = client.get(
        "/api/v1/whoami", headers={"Authorization": f"Bearer {minted.json()['token']}"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "admin",
        "type": "USER",
        "task_token": {"environment": "dev", "asset_scope": ["gold.sales"]},
    }


def test_whoami_requires_authentication(client: TestClient) -> None:
    """No credential is a 401, like every other platform route."""
    assert client.get("/api/v1/whoami").status_code == 401
