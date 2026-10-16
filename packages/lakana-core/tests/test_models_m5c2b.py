"""The artifact store's rows."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from core_test_helpers import ensure_table
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.models import (
    ArtifactBlob,
    ArtifactUpload,
    ArtifactVersion,
    AuditEventType,
    Table,
    TableType,
)

_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _artifact(session: Session) -> Table:
    """Insert an artifact row in `dev`/`models`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    Table
        The row, with `table_type` ARTIFACT.
    """
    _, table_id = ensure_table(session, "dev", "models", "churn")
    row = session.get(Table, table_id)
    assert row is not None
    row.table_type = TableType.ARTIFACT
    session.commit()
    return row


def _version(artifact_id: uuid.UUID, number: int) -> ArtifactVersion:
    """Build a version row.

    Parameters
    ----------
    artifact_id
        The artifact.
    number
        The version number.

    Returns
    -------
    ArtifactVersion
        The row, not yet added.
    """
    return ArtifactVersion(
        id=uuid.uuid4(),
        artifact_id=artifact_id,
        version=number,
        manifest_sha256="a" * 64,
        manifest_json={"format": 1, "is_directory": False, "files": []},
        size_bytes=3,
        file_count=1,
        is_directory=False,
        created_at=_NOW,
    )


def test_an_artifact_is_a_kind_of_catalog_row(session: Session) -> None:
    row = _artifact(session)
    assert row.kind == "ARTIFACT"
    assert (row.metadata_location, row.view_definition, row.external_config) == (None, None, None)


def test_a_version_round_trips_and_its_number_is_unique(session: Session) -> None:
    row = _artifact(session)
    session.add(_version(row.id, 1))
    session.commit()
    stored = session.scalars(select(ArtifactVersion)).one()
    assert (stored.version, stored.deleted_at, stored.task_run_id, stored.claim) == (
        1,
        None,
        None,
        None,
    )
    assert stored.created_at == _NOW
    session.add(_version(row.id, 1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_uploads_and_blobs_round_trip(session: Session, principal: PrincipalRef) -> None:
    row = _artifact(session)
    session.add(
        ArtifactUpload(
            id=uuid.uuid4(),
            artifact_id=row.id,
            principal_id=principal.id,
            manifest_sha256="b" * 64,
            manifest_json={},
            created_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )
    )
    session.add(ArtifactBlob(artifact_id=row.id, sha256="c" * 64, size_bytes=3, created_at=_NOW))
    session.commit()
    session.add(ArtifactBlob(artifact_id=row.id, sha256="c" * 64, size_bytes=3, created_at=_NOW))
    with pytest.raises(IntegrityError):
        session.commit()


def test_dropping_the_artifact_row_removes_its_versions_uploads_and_blobs(
    session: Session, principal: PrincipalRef
) -> None:
    row = _artifact(session)
    session.add(_version(row.id, 1))
    session.add(ArtifactBlob(artifact_id=row.id, sha256="c" * 64, size_bytes=3, created_at=_NOW))
    session.add(
        ArtifactUpload(
            id=uuid.uuid4(),
            artifact_id=row.id,
            principal_id=principal.id,
            manifest_sha256="b" * 64,
            manifest_json={},
            created_at=_NOW,
            expires_at=_NOW,
        )
    )
    session.commit()
    session.delete(row)
    session.commit()
    for model in (ArtifactVersion, ArtifactBlob, ArtifactUpload):
        assert session.scalars(select(model)).all() == []


def test_the_audit_events_exist() -> None:
    names = {
        "ARTIFACT_CREATED",
        "ARTIFACT_VERSION_ADDED",
        "ARTIFACT_DOWNLOADED",
        "ARTIFACT_VERSION_DELETED",
        "ARTIFACT_DROPPED",
    }
    assert names <= {event.value for event in AuditEventType}
    assert all(len(name) <= 64 for name in names)
