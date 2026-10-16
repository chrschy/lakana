"""The maintenance route."""

from __future__ import annotations

import inspect
import io
import uuid
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.ipc
import pytest
from fastapi.testclient import TestClient
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.update import SetSnapshotRefUpdate
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana.cli import tables as cli_tables
from lakana.resources.tables import Tables
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.maintenance import (
    DEFAULT_KEEP_DAYS,
    DEFAULT_KEEP_LAST,
    DEFAULT_ORPHAN_AGE_HOURS,
    MaintenanceReport,
)
from lakana_core.catalog.service import CatalogService, failed_snapshot_tag
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Principal, QueryRun
from lakana_core.storage.resolver import StorageResolver
from lakana_server.api.platform.schemas import MaintenanceIn
from lakana_worker.pipeline import run_one
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"


def _setup(client: TestClient) -> None:
    """Create `gold.t` and append to it twice (two snapshots).

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    created = client.post(
        _TABLES,
        headers=AUTH_HEADER,
        json={"name": "t", "schema": [{"name": "id", "type": "long", "nullable": True}]},
    )
    assert created.status_code == 201, created.text
    for value in (1, 2):
        sink = io.BytesIO()
        data = pa.table({"id": pa.array([value], pa.int64())})
        with pyarrow.ipc.new_stream(sink, data.schema) as writer:
            writer.write_table(data)
        appended = client.post(
            f"{_TABLES}/t/data",
            headers={**AUTH_HEADER, "Content-Type": "application/vnd.apache.arrow.stream"},
            content=sink.getvalue(),
        )
        assert appended.status_code == 200, appended.text


def test_maintenance_is_an_async_run_reporting_what_it_did(client: TestClient) -> None:
    """`POST …/maintenance` queues a run; its status carries the report."""
    _setup(client)

    submitted = client.post(
        f"{_TABLES}/t/maintenance",
        headers=AUTH_HEADER,
        json={"keep_days": 0, "keep_last": 1, "dry_run": True, "older_than_hours": 1},
    )
    assert submitted.status_code == 202, submitted.text
    query_id = submitted.json()["query_id"]
    with Session(client.app.state.engine) as session:
        run_one(session, uuid.UUID(query_id), GrantAuthorization(session))
    status = client.get(f"/api/v1/environments/dev/queries/{query_id}", headers=AUTH_HEADER).json()

    assert status["status"] == "SUCCEEDED", status
    assert status["write_mode"] == "maintain"
    assert status["target"] == "gold.t"
    report = status["maintenance"]["report"]
    assert report["dry_run"] is True
    assert len(report["snapshots_expired"]) == 1
    assert report["tags_removed"] == []
    assert status["maintenance"]["request"] == {
        "keep_days": 0,
        "keep_last": 1,
        "dry_run": True,
        "older_than_hours": 1,
    }


def test_the_status_names_the_tag_of_a_failed_check_that_maintenance_removed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight days on, the run's status carries the removed tag's name and the failed snapshot."""
    _setup(client)
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="t")
    tag = failed_snapshot_tag(uuid.UUID(int=7))
    with Session(client.app.state.engine) as session:
        row = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
        admin = PrincipalRef(id=row.id, name=row.name, type=row.type)
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        *_, middle, failed = sorted(
            catalog.load_table(admin, ref).metadata.snapshots, key=lambda s: s.timestamp_ms
        )
        catalog.commit_table(
            admin,
            ref,
            requirements=[],
            updates=[
                SetSnapshotRefUpdate(
                    ref_name=tag, type=SnapshotRefType.TAG, snapshot_id=failed.snapshot_id
                ),
                SetSnapshotRefUpdate(
                    ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=middle.snapshot_id
                ),
            ],
            internal_rollback=True,  # the rollback is the one setter of such a tag
        )
    real_now = datetime.now(UTC)
    real_maintain = CatalogService.maintain

    def eight_days_later(
        self: CatalogService, p: PrincipalRef, ref: TableRef, **kwargs: int | bool
    ) -> MaintenanceReport:
        """Run the maintenance as if it were eight days later, for this method only.

        Parameters
        ----------
        self
            The catalog service.
        p
            The caller.
        ref
            The table.
        **kwargs
            The maintenance options, passed on.

        Returns
        -------
        MaintenanceReport
            What the maintenance did.
        """
        return real_maintain(
            self,
            p,
            ref,
            now=real_now + timedelta(days=8),
            **kwargs,  # ty: ignore[invalid-argument-type]
        )

    monkeypatch.setattr(CatalogService, "maintain", eight_days_later)
    submitted = client.post(f"{_TABLES}/t/maintenance", headers=AUTH_HEADER, json={})
    assert submitted.status_code == 202, submitted.text
    query_id = submitted.json()["query_id"]
    with Session(client.app.state.engine) as session:
        run_one(session, uuid.UUID(query_id), GrantAuthorization(session))
    status = client.get(f"/api/v1/environments/dev/queries/{query_id}", headers=AUTH_HEADER).json()

    report = status["maintenance"]["report"]
    assert status["status"] == "SUCCEEDED", status
    assert report["tags_removed"] == [tag]
    assert report["snapshots_expired"] == [failed.snapshot_id]


def test_the_defaults_are_seven_days_five_snapshots_and_seventy_two_hours(
    client: TestClient,
) -> None:
    """An empty body means `{keep_days: 7, keep_last: 5, dry_run: false, older_than_hours: 72}`."""
    _setup(client)

    submitted = client.post(f"{_TABLES}/t/maintenance", headers=AUTH_HEADER, json={})
    status = client.get(
        f"/api/v1/environments/dev/queries/{submitted.json()['query_id']}", headers=AUTH_HEADER
    ).json()

    assert status["maintenance"]["request"] == {
        "keep_days": 7,
        "keep_last": 5,
        "dry_run": False,
        "older_than_hours": 72,
    }


def test_a_custom_older_than_hours_is_carried_to_the_run(client: TestClient) -> None:
    """`older_than_hours` is recorded in the run's request for the worker."""
    _setup(client)

    submitted = client.post(
        f"{_TABLES}/t/maintenance", headers=AUTH_HEADER, json={"older_than_hours": 24}
    )
    status = client.get(
        f"/api/v1/environments/dev/queries/{submitted.json()['query_id']}", headers=AUTH_HEADER
    ).json()

    assert status["maintenance"]["request"]["older_than_hours"] == 24


@pytest.mark.parametrize(
    "body",
    [
        {"keep_days": -1},
        {"keep_last": 0},
        {"older_than_hours": 0},
        {"older_than_hours": 87601},
        {"keep_days": 7, "purge": True},
    ],
)
def test_an_invalid_maintenance_body_is_a_422(client: TestClient, body: dict[str, object]) -> None:
    """`keep_days` ≥ 0, `keep_last` ≥ 1, 1 ≤ `older_than_hours` ≤ 87600, no unknown fields."""
    _setup(client)

    response = client.post(f"{_TABLES}/t/maintenance", headers=AUTH_HEADER, json=body)

    assert response.status_code == 422


def test_maintaining_a_missing_table_is_a_404(client: TestClient) -> None:
    """Refused at submission, not left for the worker."""
    create_environment(client)

    response = client.post(f"{_TABLES}/nope/maintenance", headers=AUTH_HEADER, json={})

    assert response.status_code == 404
    assert "gold.nope" in response.text


@pytest.mark.parametrize(
    "note",
    [
        "table changed during maintenance, retry",
        "referenced paths outside the table location; orphan removal skipped.",
    ],
)
def test_a_skipped_part_reaches_the_status_as_the_reports_note(
    client: TestClient, note: str
) -> None:
    """The status passes the report's `note` through untouched."""
    _setup(client)
    query_id = client.post(f"{_TABLES}/t/maintenance", headers=AUTH_HEADER, json={}).json()[
        "query_id"
    ]
    with Session(client.app.state.engine) as session:
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        assert run.maintenance is not None
        run.maintenance = {
            "request": run.maintenance["request"],
            "report": {"dry_run": False, "snapshots_expired": [], "note": note},
        }
        session.commit()

    status = client.get(f"/api/v1/environments/dev/queries/{query_id}", headers=AUTH_HEADER).json()

    assert status["maintenance"]["report"]["note"] == note


def test_a_conflict_reaches_the_status_as_a_machine_readable_field(client: TestClient) -> None:
    """A real maintenance run's report carries `conflict` through the status, false here."""
    _setup(client)
    query_id = client.post(
        f"{_TABLES}/t/maintenance", headers=AUTH_HEADER, json={"dry_run": True}
    ).json()["query_id"]
    with Session(client.app.state.engine) as session:
        run_one(session, uuid.UUID(query_id), GrantAuthorization(session))

    status = client.get(f"/api/v1/environments/dev/queries/{query_id}", headers=AUTH_HEADER).json()

    assert status["maintenance"]["report"]["conflict"] is False


def _principal_with(client: TestClient, name: str, grant: dict[str, str]) -> dict[str, str]:
    """Create a principal holding one grant on `dev` and return its auth header.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    grant
        The grant's `role` or `action` entry.

    Returns
    -------
    dict of str to str
        Its `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": name, **grant, "kind": "ENVIRONMENT", "environment": "dev"},
    )
    assert granted.status_code == 201, granted.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


@pytest.mark.parametrize(
    ("name", "grant"),
    [
        ("analyst", {"role": "ANALYST"}),
        ("maintainer", {"action": "MAINTAIN_TABLE"}),
    ],
)
def test_maintenance_without_maintain_table_or_submit_query_is_403_without_leaking_existence(
    client: TestClient, name: str, grant: dict[str, str]
) -> None:
    """ANALYST lacks MAINTAIN_TABLE; a bare MAINTAIN_TABLE grant lacks SUBMIT_QUERY.

    Both get the same 403 for an existing table and a missing one, so the
    refusal says nothing about which tables exist.
    """
    _setup(client)
    headers = _principal_with(client, name, grant)

    existing = client.post(f"{_TABLES}/t/maintenance", headers=headers, json={})
    missing = client.post(f"{_TABLES}/nope/maintenance", headers=headers, json={})

    assert existing.status_code == 403, existing.text
    assert missing.status_code == 403, missing.text
    assert "nope" not in missing.text
    assert existing.json() == missing.json()
    assert existing.json()["type"] == "PermissionDenied"


def test_the_sdk_and_cli_defaults_match_the_server() -> None:
    """Drift guard: `keep_days` 7, `keep_last` 5, `older_than_hours` 72 on every surface."""
    expected = {
        "keep_days": DEFAULT_KEEP_DAYS,
        "keep_last": DEFAULT_KEEP_LAST,
        "older_than_hours": DEFAULT_ORPHAN_AGE_HOURS,
    }
    sdk = inspect.signature(Tables.maintain).parameters
    cli = inspect.signature(cli_tables.maintain).parameters
    body = MaintenanceIn()

    assert {key: sdk[key].default for key in expected} == expected
    assert {key: cli[key].default for key in expected} == expected
    assert {key: getattr(body, key) for key in expected} == expected
    assert expected == {"keep_days": 7, "keep_last": 5, "older_than_hours": 72}
