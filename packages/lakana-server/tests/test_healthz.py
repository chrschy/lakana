"""Tests for the unauthenticated health endpoint."""

from fastapi.testclient import TestClient


def test_healthz_needs_no_credentials(client: TestClient) -> None:
    """`GET /healthz` answers 200 with no Authorization header."""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_not_part_of_the_platform_api(client: TestClient) -> None:
    """It lives outside /api/v1, so the SDK route contract doesn't cover it."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/healthz" not in paths
