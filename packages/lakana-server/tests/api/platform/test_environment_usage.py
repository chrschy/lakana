"""Tests for `GET /api/v1/environments/{name}/usage`: figures from the catalog database only."""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import Environment, JobVersion, Repository, RepositoryKind
from tests.api.platform.test_environments import _create, _grant_environment_admin, _token_for
from tests.conftest import AUTH_HEADER

_EMPTY = {"count": 0, "bytes": 0, "unmeasured": 0}


def test_an_environment_admin_sees_what_the_environment_uses(
    client: TestClient, tmp_path: Path
) -> None:
    _create(client, tmp_path)
    with Session(client.app.state.engine) as session:
        environment = session.scalars(select(Environment).where(Environment.name == "dev")).one()
        repository = Repository(
            id=uuid.uuid4(), name="sales", kind=RepositoryKind.HTTPS, url="https://e.com/r.git"
        )
        session.add(repository)
        session.flush()
        for code_bytes in (1500, None):
            session.add(
                JobVersion(
                    id=uuid.uuid4(),
                    repository_id=repository.id,
                    environment_id=environment.id,
                    commit_sha=uuid.uuid4().hex + "0" * 8,
                    manifest_json={},
                    code_uri="file:///x.tar.gz",
                    code_sha256="0" * 64,
                    code_bytes=code_bytes,
                )
            )
        session.commit()

    found = client.get(
        "/api/v1/environments/dev/usage", headers=_grant_environment_admin(client, "dev")
    )

    assert found.status_code == 200, found.text
    assert found.json() == {
        "environment": "dev",
        "tables": _EMPTY,
        "dropped_tables": _EMPTY,
        "artifacts": _EMPTY,
        "code_archives": {"count": 2, "bytes": 1500, "unmeasured": 1},
        "total_bytes": 1500,
        "complete": False,
    }


def test_the_usage_is_refused_to_a_reader_of_the_environment(
    client: TestClient, tmp_path: Path
) -> None:
    _create(client, tmp_path)
    reader = _token_for(client, "reader")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "reader",
            "role": "ANALYST",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert granted.status_code == 201, granted.text
    assert client.get("/api/v1/environments/dev", headers=reader).status_code == 200
    assert client.get("/api/v1/environments/dev/usage", headers=reader).status_code == 403
    assert client.get("/api/v1/environments/dev/usage").status_code == 401


def test_the_usage_of_an_unknown_environment_is_not_found(client: TestClient) -> None:
    found = client.get("/api/v1/environments/nowhere/usage", headers=AUTH_HEADER)
    assert found.status_code == 404
    assert "nowhere" in found.text
