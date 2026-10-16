"""Views are not exposed over the Iceberg REST catalog."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.grants.authorization import GrantAuthorization
from lakana_worker.pipeline import run_one
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment


def test_the_iceberg_catalog_neither_lists_nor_loads_a_view(client: TestClient) -> None:
    """External engines see tables only."""
    create_environment(client)
    submitted = client.post(
        "/api/v1/environments/dev/queries",
        headers=AUTH_HEADER,
        json={"sql": "SELECT 1 AS id", "target": {"table": "gold.v", "mode": "view"}},
    )
    with Session(client.app.state.engine) as session:
        run_one(session, uuid.UUID(submitted.json()["query_id"]), GrantAuthorization(session))

    listed = client.get("/v1/dev/namespaces/gold/tables", headers=AUTH_HEADER)
    loaded = client.get("/v1/dev/namespaces/gold/tables/v", headers=AUTH_HEADER)

    assert listed.status_code == 200, listed.text
    assert listed.json()["identifiers"] == []
    assert loaded.status_code == 404
