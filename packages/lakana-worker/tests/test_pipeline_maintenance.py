# packages/lakana-worker/tests/test_pipeline_maintenance.py
"""Maintenance runs in the worker."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.table import StaticTable
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.update import SetSnapshotRefUpdate
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.maintenance import MaintenanceReport
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService, failed_snapshot_tag
from lakana_core.config import RunConfig, StorageConfig
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Principal,
    PrincipalType,
    QueryErrorCause,
    QueryRunStatus,
    WriteMode,
)
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import pipeline
from lakana_worker.pipeline import run_one


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, persisted.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        `admin`.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    return PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)


def test_a_maintenance_run_expires_snapshots_and_reports(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The worker runs `write_mode = maintain` without parsing any SQL."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ref = TableRef.from_dotted("dev", "gold.daily")
    for index, mode in enumerate([WriteMode.CREATE, WriteMode.APPEND, WriteMode.APPEND]):
        data = pa.table({"id": pa.array([index], pa.int64())})
        catalog.write_batches(
            principal, ref, mode=mode, schema=data.schema, batches=data.to_batches()
        )
    run = QueryService(session, AllowAllAuthorization()).submit_maintenance(
        principal, "dev", "gold.daily", keep_days=0, keep_last=1
    )

    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.maintenance is not None
    report = run.maintenance["report"]
    assert len(report["snapshots_expired"]) == 2
    assert report["tags_removed"] == []
    assert report["files_deleted"] == 0
    assert len(catalog.load_table(principal, ref).metadata.snapshots) == 1
    completed = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
    ).scalar_one()
    assert completed.detail["status"] == "SUCCEEDED"


def test_a_maintenance_run_removes_the_old_tag_of_a_failed_check_and_names_it(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's run removes a tag and its snapshot eight days on; the report names the tag."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ref = TableRef.from_dotted("dev", "gold.daily")
    for index, mode in enumerate([WriteMode.CREATE, WriteMode.APPEND, WriteMode.APPEND]):
        data = pa.table({"id": pa.array([index], pa.int64())})
        catalog.write_batches(
            principal, ref, mode=mode, schema=data.schema, batches=data.to_batches()
        )
    *_, middle, failed = sorted(
        catalog.load_table(principal, ref).metadata.snapshots, key=lambda s: s.timestamp_ms
    )
    tag = failed_snapshot_tag(uuid.UUID(int=7))
    catalog.commit_table(
        principal,
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
    run = QueryService(session, AllowAllAuthorization()).submit_maintenance(
        principal, "dev", "gold.daily"
    )

    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.maintenance is not None
    report = run.maintenance["report"]
    assert report["tags_removed"] == [tag]
    assert report["snapshots_expired"] == [failed.snapshot_id]
    assert tag not in catalog.load_table(principal, ref).metadata.refs


def test_a_maintenance_failure_never_leaks_a_storage_credential(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review minor 2: the maintenance branch registers credential values for redaction.

    A real secret-backed environment; `maintain` is patched to fail the way
    a storage backend echoing its credential back would.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([1], pa.int64())})
    catalog.write_batches(
        principal,
        TableRef.from_dotted("dev", "gold.daily"),
        mode=WriteMode.CREATE,
        schema=data.schema,
        batches=data.to_batches(),
    )
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    SecretService(session, AllowAllAuthorization(), provider).create(
        principal,
        environment="dev",
        name="creds",
        values={"secret_access_key": "MAINTENANCE_SECRET_MUST_NOT_LEAK"},
    )
    catalog.update_environment_storage(principal, "dev", secret_ref="creds")

    def failing_maintain(self: CatalogService, *args: object, **kwargs: object) -> None:
        raise RuntimeError("Listing failed: secret_access_key=MAINTENANCE_SECRET_MUST_NOT_LEAK")

    monkeypatch.setattr(CatalogService, "maintain", failing_maintain)
    run = QueryService(session, AllowAllAuthorization()).submit_maintenance(
        principal, "dev", "gold.daily"
    )

    run_one(session, run.id, AllowAllAuthorization(), provider)
    session.refresh(run)

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert "Listing failed:" in run.error_message
    assert "<redacted>" in run.error_message
    assert "MAINTENANCE_SECRET_MUST_NOT_LEAK" not in run.error_message


def test_the_maintenance_timeout_defaults_to_thirty_minutes() -> None:
    """The setting exists and its default works without configuration."""
    assert RunConfig().maintenance_timeout == 1800


def test_a_maintenance_run_past_its_deadline_stops_between_steps_and_the_next_run_finishes(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline ends the run after the expiry: a timeout failure that says how to go on."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ref = TableRef.from_dotted("dev", "gold.daily")
    for index, mode in enumerate([WriteMode.CREATE, WriteMode.APPEND, WriteMode.APPEND]):
        data = pa.table({"id": pa.array([index], pa.int64())})
        catalog.write_batches(
            principal, ref, mode=mode, schema=data.schema, batches=data.to_batches()
        )
    stray = Path(catalog.load_table(principal, ref).metadata.location.removeprefix("file://"))
    stray = stray / "data" / "stray.parquet"
    stray.write_bytes(b"x")
    real_maintain = CatalogService.maintain

    def aged(self: CatalogService, p: PrincipalRef, ref: TableRef, **kwargs: object) -> object:
        """Run the maintenance as if the files were old, passing the stop signal on.

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
        object
            What the maintenance did.
        """
        return real_maintain(
            self,
            p,
            ref,
            now=datetime.now(UTC) + timedelta(days=30),
            **kwargs,  # ty: ignore[invalid-argument-type]
        )

    monkeypatch.setattr(CatalogService, "maintain", aged)
    # Time stands still for the start and the first question, then jumps past the limit.
    ticks = iter([0.0, 0.0])
    monkeypatch.setattr(pipeline, "_clock", lambda: next(ticks, 10_000.0))
    monkeypatch.setattr(pipeline, "_MAINTENANCE_TIMEOUT", 60.0)
    service = QueryService(session, AllowAllAuthorization())
    run = service.submit_maintenance(principal, "dev", "gold.daily", keep_days=0, keep_last=1)

    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)

    assert run.status == QueryRunStatus.FAILED
    assert run.error_cause == QueryErrorCause.TIMEOUT.value
    assert run.error_message is not None
    assert "LAKANA_RUN__MAINTENANCE_TIMEOUT" in run.error_message
    assert "lakana table maintain dev gold.daily" in run.error_message
    assert run.maintenance is not None
    assert len(run.maintenance["report"]["snapshots_expired"]) == 2
    assert run.maintenance["report"]["stopped"] is True
    assert "next run" in run.maintenance["report"]["note"]
    assert stray.exists()
    loaded = catalog.load_table(principal, ref)
    assert len(loaded.metadata.snapshots) == 1
    assert StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow().num_rows == 3

    monkeypatch.setattr(pipeline, "_clock", lambda: 0.0)
    again = service.submit_maintenance(principal, "dev", "gold.daily", keep_days=0, keep_last=1)
    run_one(session, again.id, AllowAllAuthorization())
    session.refresh(again)
    assert again.status == QueryRunStatus.SUCCEEDED, again.error_message
    assert not stray.exists()
