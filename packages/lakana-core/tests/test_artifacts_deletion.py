"""Deleting versions and artifacts never removes bytes a live version needs.

The order of effects is rows first, bytes second, with the artifact's lock held for the
decision: a crash leaves bytes without rows (the sweep's garbage) and never a row without
bytes for a live version.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    ActionAllowList,
    artifact_document,
    artifact_service,
    ensure_table,
    python_task_run,
    store_version,
)
from sqlalchemy import delete, event, select, update
from sqlalchemy.orm import ORMExecuteState, Session

from lakana_core.artifacts.service import (
    DELETE_FROM_RUN_MESSAGE,
    KEPT_UNKNOWN_MANIFESTS,
    ArtifactService,
    ArtifactSource,
)
from lakana_core.artifacts.store import (
    _STREAM_ROWS,
    artifact_prefix,
    delete_quietly,
    lock_artifact,
    manifest_path,
)
from lakana_core.artifacts.sweep import sweep_artifacts
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    BoundTask,
    PrincipalRef,
    RunContext,
    SecurableKind,
    TaskScope,
)
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.roles import Role
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    ArtifactBlob,
    ArtifactUpload,
    ArtifactVersion,
    AuditEvent,
    AuditEventType,
    Environment,
    Grant,
    LineageEdge,
    Table,
)
from lakana_core.storage.base import StoredObject, StreamWriter
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver

_SHARED = b"shared weights"
_ONE = {"model.json": b"one", "weights.bin": _SHARED}
_TWO = {"model.json": b"two", "weights.bin": _SHARED}
_THREE = {"model.json": b"three", "weights.bin": _SHARED}
_OTHER = TableRef("dev", ARTIFACT_REF.namespace, "other")


def _sha(data: bytes) -> str:
    """Hash bytes.

    Parameters
    ----------
    data
        The bytes.

    Returns
    -------
    str
        The SHA-256, hex.
    """
    return hashlib.sha256(data).hexdigest()


def _stored(session: Session, name: str = "churn") -> set[str]:
    """Return the names of the objects under one artifact's directory.

    Parameters
    ----------
    session
        Database session.
    name
        The artifact's name in `models`.

    Returns
    -------
    set of str
        ``blobs/<sha256>`` and ``manifests/<n>.json`` names.
    """
    artifact_id = session.scalars(select(Table.id).where(Table.name == name)).one()
    storage = StorageResolver(session).for_environment("dev").storage
    prefix = artifact_prefix(artifact_id) + "/"
    return {path[len(prefix) :] for path in storage.list(artifact_prefix(artifact_id))}


def _read(service: ArtifactService, p: PrincipalRef, version: int, data: bytes) -> bytes:
    """Download one file of a version.

    Parameters
    ----------
    service
        The service.
    p
        The reader.
    version
        The version.
    data
        The contents expected, for its hash.

    Returns
    -------
    bytes
        What the store returned.
    """
    return b"".join(service.open_blob(p, ARTIFACT_REF, version, _sha(data)).chunks)


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """List the audit events of one type, oldest first.

    Parameters
    ----------
    session
        Database session.
    kind
        The event type.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == kind)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )


def _before_lock(
    monkeypatch: pytest.MonkeyPatch, action: Callable[[], object], *, skip: int = 0
) -> None:
    """Run `action` once, right before the `skip + 1`-th lock of the artifact is taken.

    Parameters
    ----------
    monkeypatch
        The fixture.
    action
        What another request does at that moment.
    skip
        How many locks to let through first.
    """
    real = lock_artifact
    state = {"seen": 0, "fired": False}

    def hooked(session: Session, artifact_id: uuid.UUID) -> bool:
        if not state["fired"]:
            if state["seen"] == skip:
                state["fired"] = True
                action()
            else:
                state["seen"] += 1
        return real(session, artifact_id)

    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", hooked)


def test_deleting_a_version_keeps_a_file_another_version_shares(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A blob goes only when no live version references it."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)

    assert (deleted.version, deleted.deleted_by, deleted.files) == (1, "admin", ())
    assert deleted.deleted_at is not None
    assert _stored(session) == {
        f"blobs/{_sha(b'two')}",
        f"blobs/{_sha(_SHARED)}",
        "manifests/2.json",
    }
    assert _read(service, principal, 2, _SHARED) == _SHARED
    assert _read(service, principal, 2, b"two") == b"two"
    with pytest.raises(NotFoundError) as gone:
        service.open_blob(principal, ARTIFACT_REF, 1, _sha(b"one"))
    assert str(gone.value) == (
        "Version 1 of models.churn was deleted; its bytes are gone. "
        "Read a version that exists: `lakana artifact versions list models.churn --env dev` "
        "shows which."
    )
    assert service.get_version(principal, ARTIFACT_REF, 1).files == ()
    assert [
        (v.version, v.deleted_at is None) for v in service.list_versions(principal, ARTIFACT_REF)
    ] == [
        (2, True),
        (1, False),
    ]
    assert set(session.scalars(select(ArtifactBlob.sha256))) == {_sha(b"two"), _sha(_SHARED)}
    (event,) = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert event.detail == {
        "artifact": "models.churn",
        "version": 1,
        "reason": "requested",
        "size_bytes": len(b"one") + len(_SHARED),
        "freed_files": 1,
        "principal": "admin",
    }


def test_deleting_the_newest_version_makes_the_one_before_current_and_never_reuses_a_number(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    service.delete_version(principal, ARTIFACT_REF, 2)
    view = service.get_artifact(principal, ARTIFACT_REF)
    assert (view.current_version, view.versions, view.size_bytes) == (
        1,
        2,
        len(b"one") + len(_SHARED),
    )
    assert service.get_version(principal, ARTIFACT_REF).version == 1
    third = store_version(service, principal, ARTIFACT_REF, _THREE)
    assert third.version.version == 3
    service.delete_version(principal, ARTIFACT_REF, 3)
    assert store_version(service, principal, ARTIFACT_REF, _TWO).version.version == 4
    service.delete_version(principal, ARTIFACT_REF, 1)
    service.delete_version(principal, ARTIFACT_REF, 4)
    with pytest.raises(NotFoundError, match="has no version yet"):
        service.get_version(principal, ARTIFACT_REF)
    assert _stored(session) == set()
    with pytest.raises(ConflictError, match=r"Version 3 of models.churn is already deleted."):
        service.delete_version(principal, ARTIFACT_REF, 3)
    with pytest.raises(NotFoundError, match=r"models.churn has no version 9."):
        service.delete_version(principal, ARTIFACT_REF, 9)
    assert store_version(service, principal, ARTIFACT_REF, _ONE).version.version == 5


def test_an_open_upload_protects_its_files_from_deletion(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A commit that needs a blob either sees it or re-uploads it."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    upload = service.begin_upload(
        principal, ARTIFACT_REF, artifact_document({"m": b"one", "w": _SHARED, "x": b"two"})
    )
    assert upload.missing == (_sha(b"two"),)
    service.delete_version(principal, ARTIFACT_REF, 1)
    # The upload names `one` and the shared file too: all stay, only the manifest goes.
    assert _stored(session) == {f"blobs/{_sha(b'one')}", f"blobs/{_sha(_SHARED)}"}
    (event,) = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert event.detail["freed_files"] == 0
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"two"), [b"two"])
    committed = service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert committed.version.version == 2
    assert _read(service, principal, 2, _SHARED) == _SHARED
    assert _read(service, principal, 2, b"one") == b"one"
    assert _read(service, principal, 2, b"two") == b"two"


def test_an_expired_upload_protects_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    now = [datetime(2026, 10, 5, 12, 0, tzinfo=UTC)]
    service = artifact_service(session, tmp_path, clock=lambda: now[0])
    store_version(service, principal, ARTIFACT_REF, _ONE)
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"one"}))
    now[0] += timedelta(hours=2)
    service.delete_version(principal, ARTIFACT_REF, 1)
    assert _stored(session) == set()


def test_a_deleted_versions_record_and_lineage_remain(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    ensure_table(session, "dev", "silver", "rows")
    store_version(service, principal, ARTIFACT_REF, _ONE, sources=[ArtifactSource("silver.rows")])
    service.delete_version(principal, ARTIFACT_REF, 1)
    row = session.scalars(select(ArtifactVersion)).one()
    assert (row.version, row.deleted_by, row.manifest_sha256 != "") == (1, principal.id, True)
    edge = session.scalars(select(LineageEdge)).one()
    assert (edge.source_name, edge.target_name, edge.target_snapshot_id) == (
        "silver.rows",
        "models.churn",
        1,
    )


def _producer(session: Session, principal: PrincipalRef, keep: int | None) -> PrincipalRef:
    """Return the principal of a running attempt that produces `models.churn`.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    keep
        The asset's `keep_versions`.

    Returns
    -------
    PrincipalRef
        The principal as the attempt's bound token authenticates it.
    """
    ensure_table(session, "dev", "silver", "rows")
    task = python_task_run(
        session,
        principal.id,
        asset="models.churn",
        inputs={"rows": "silver.rows"},
        extra={"output": "artifact", "keep_versions": keep},
    )
    bound = BoundTask(
        task.id, 1, "models.churn", ("silver.rows",), ("silver.rows",), "artifact", keep
    )
    return dataclasses.replace(
        principal,
        task_scope=TaskScope("dev", [], uuid.uuid4(), bound),
        run_context=RunContext(task_run_id=task.id, claim=1),
    )


def test_keep_versions_prunes_at_commit_as_the_same_principal(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """`keep_versions=2` leaves two versions' bytes after three commits."""
    service = artifact_service(session, tmp_path)
    actor = _producer(session, principal, 2)
    assert store_version(service, actor, ARTIFACT_REF, _ONE).deleted_versions == ()
    assert store_version(service, actor, ARTIFACT_REF, _TWO).deleted_versions == ()
    third = store_version(service, actor, ARTIFACT_REF, _THREE)
    assert third.deleted_versions == (1,)
    assert _stored(session) == {
        f"blobs/{_sha(b'two')}",
        f"blobs/{_sha(b'three')}",
        f"blobs/{_sha(_SHARED)}",
        "manifests/2.json",
        "manifests/3.json",
    }
    rows = {v.version: v for v in session.scalars(select(ArtifactVersion))}
    assert rows[1].deleted_at is not None
    assert rows[1].deleted_by == principal.id
    assert (rows[2].deleted_at, rows[3].deleted_at) == (None, None)
    (event,) = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert (event.detail["version"], event.detail["reason"]) == (1, "keep_versions")
    assert event.detail["freed_files"] == 1


def test_retention_never_comes_from_the_request_and_never_applies_outside_a_task(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """An upload without a bound token prunes nothing; `None` keeps all."""
    service = artifact_service(session, tmp_path)
    keep_all = _producer(session, principal, None)
    for files in (_ONE, _TWO, _THREE):
        assert store_version(service, keep_all, ARTIFACT_REF, files).deleted_versions == ()
    assert store_version(service, principal, ARTIFACT_REF, {"m": b"four"}).deleted_versions == ()
    assert service.get_artifact(principal, ARTIFACT_REF).versions == 4
    live = session.scalars(select(ArtifactVersion).where(ArtifactVersion.deleted_at.is_(None)))
    assert len(live.all()) == 4


@pytest.mark.parametrize("keep", [0, -1, True, False, "2", 1.5], ids=repr)
def test_a_retention_that_is_not_a_positive_number_prunes_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef, keep: object
) -> None:
    """A token carries what the manifest said; whatever it carries, a commit never loses all."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    actor = _producer(session, principal, None)
    assert actor.task_scope is not None and actor.task_scope.bound is not None
    bound = dataclasses.replace(actor.task_scope.bound, keep_versions=keep)
    actor = dataclasses.replace(
        actor, task_scope=dataclasses.replace(actor.task_scope, bound=bound)
    )
    result = store_version(service, actor, ARTIFACT_REF, _TWO)
    assert result.deleted_versions == ()
    assert service.get_artifact(principal, ARTIFACT_REF).versions == 2
    assert _read(service, principal, 2, b"two") == b"two"


def test_keep_one_prunes_several_at_once(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    for files in (_ONE, _TWO):
        store_version(service, principal, ARTIFACT_REF, files)
    actor = _producer(session, principal, 1)
    third = store_version(service, actor, ARTIFACT_REF, _THREE)
    assert third.deleted_versions == (2, 1)
    assert _stored(session) == {
        f"blobs/{_sha(b'three')}",
        f"blobs/{_sha(_SHARED)}",
        "manifests/3.json",
    }
    events = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert sorted((e.detail["version"], e.detail["freed_files"]) for e in events) == [
        (1, 1),
        (2, 1),
    ]


def test_pruning_keeps_the_files_of_an_open_upload(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """`keep_versions` prunes while another upload is open."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"only in one"})
    waiting = service.begin_upload(
        principal, ARTIFACT_REF, artifact_document({"a": b"only in one"})
    )
    assert waiting.missing == ()
    actor = _producer(session, principal, 1)
    assert store_version(service, actor, ARTIFACT_REF, {"b": b"b"}).deleted_versions == (1,)
    assert f"blobs/{_sha(b'only in one')}" in _stored(session)
    committed = service.commit_upload(principal, ARTIFACT_REF, waiting.upload_id)
    assert _read(service, principal, committed.version.version, b"only in one") == b"only in one"


def test_a_commit_that_waits_for_the_lock_prunes_after_the_one_before_it(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point F: one commit prunes while another waits; the waiting one's files survive."""
    service = artifact_service(session, tmp_path)
    first, second = _producer(session, principal, 1), _producer(session, principal, 1)
    a_files, b_files = {"a": b"a", "s": _SHARED}, {"b": b"b", "s": _SHARED}
    upload_a = service.begin_upload(first, ARTIFACT_REF, artifact_document(a_files))
    upload_b = service.begin_upload(second, ARTIFACT_REF, artifact_document(b_files))
    for upload, files, actor in ((upload_a, a_files, first), (upload_b, b_files, second)):
        by_hash = {_sha(data): data for data in files.values()}
        for digest in upload.missing:
            service.put_blob(actor, ARTIFACT_REF, upload.upload_id, digest, [by_hash[digest]])
    results: list[tuple[int, ...]] = []
    _before_lock(
        monkeypatch,
        lambda: results.append(
            service.commit_upload(first, ARTIFACT_REF, upload_a.upload_id).deleted_versions
        ),
    )
    result = service.commit_upload(second, ARTIFACT_REF, upload_b.upload_id)
    assert results == [()]
    assert result.deleted_versions == (1,)
    assert result.version.version == 2
    assert _stored(session) == {
        f"blobs/{_sha(b'b')}",
        f"blobs/{_sha(_SHARED)}",
        "manifests/2.json",
    }
    assert _read(service, principal, 2, b"b") == b"b"
    assert _read(service, principal, 2, _SHARED) == _SHARED


def test_deleting_needs_drop_table_and_a_task_token_never_has_it(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    reader = ArtifactService(
        session,
        StorageResolver(session),
        ActionAllowList(Action.READ_TABLE, Action.WRITE_TABLE, Action.CREATE_TABLE),
    )
    for call in (
        lambda: reader.delete_version(principal, ARTIFACT_REF, 1),
        lambda: reader.drop_artifact(principal, ARTIFACT_REF),
    ):
        with pytest.raises(PermissionDenied, match="may not DROP_TABLE"):
            call()
    assert _read(service, principal, 1, b"one") == b"one"


def test_a_principal_with_a_run_context_never_deletes_even_when_authorization_allows(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point A: the guard leaves an unprotected environment's run context to grants."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    task = python_task_run(
        session, principal.id, asset="models.churn", extra={"output": "artifact"}
    )
    claim = RunContext(task_run_id=task.id, claim=1)
    in_process = RunContext(task_run_id=task.id, query_run_id=uuid.uuid4())
    bound = _producer(session, principal, 1)
    headless = dataclasses.replace(bound, run_context=None)
    before = len(session.scalars(select(AuditEvent.id)).all())
    for actor in (
        bound,
        headless,
        dataclasses.replace(principal, run_context=claim),
        dataclasses.replace(principal, run_context=in_process),
        dataclasses.replace(principal, run_context=RunContext(task_run_id=task.id)),
    ):
        for call in (
            lambda actor=actor: service.delete_version(actor, ARTIFACT_REF, 1),
            lambda actor=actor: service.drop_artifact(actor, ARTIFACT_REF),
        ):
            with pytest.raises(PermissionDenied) as caught:
                call()
            assert str(caught.value) == DELETE_FROM_RUN_MESSAGE
    assert len(session.scalars(select(AuditEvent.id)).all()) == before
    assert _read(service, principal, 1, b"one") == b"one"
    assert session.scalars(select(Table).where(Table.name == "churn")).one() is not None


def test_the_run_context_refusal_comes_after_authorization_and_before_any_lookup(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    actor = _producer(session, principal, None)
    nobody = ArtifactService(session, StorageResolver(session), ActionAllowList())
    ghost = TableRef("dev", ARTIFACT_REF.namespace, "ghost")
    for call in (
        lambda: nobody.delete_version(actor, ghost, 1),
        lambda: nobody.drop_artifact(actor, ghost),
    ):
        with pytest.raises(PermissionDenied, match="may not DROP_TABLE"):
            call()
    # With the privilege, the refusal is the same whether the name exists or not.
    for ref in (ARTIFACT_REF, ghost):
        with pytest.raises(PermissionDenied) as caught:
            service.delete_version(actor, ref, 1)
        assert str(caught.value) == DELETE_FROM_RUN_MESSAGE


def test_a_protected_environment_needs_break_glass_to_delete(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=True))
    session.commit()
    for call in (
        lambda: service.delete_version(principal, ARTIFACT_REF, 1),
        lambda: service.drop_artifact(principal, ARTIFACT_REF),
    ):
        with pytest.raises(PermissionDenied, match="is protected"):
            call()
    denied = _events(session, AuditEventType.PROTECTED_WRITE_DENIED)
    assert [e.detail["operation"] for e in denied] == ["delete_artifact_version", "drop_artifact"]
    assert _read(service, principal, 1, b"one") == b"one"
    glass = dataclasses.replace(principal, break_glass_reason="remove a leaked training set")
    service.delete_version(glass, ARTIFACT_REF, 1)
    # The guard records a break-glass write once per target for the life of a service.
    fresh = ArtifactService(session, StorageResolver(session), AllowAllAuthorization())
    fresh.drop_artifact(glass, ARTIFACT_REF)
    operations = [e.detail["operation"] for e in _events(session, AuditEventType.BREAK_GLASS_WRITE)]
    assert operations == ["delete_artifact_version", "drop_artifact"]


def test_a_refused_delete_audits_nothing_but_what_the_guard_audits(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    service.delete_version(principal, ARTIFACT_REF, 1)
    before = len(session.scalars(select(AuditEvent.id)).all())
    for call in (
        lambda: service.delete_version(principal, ARTIFACT_REF, 1),
        lambda: service.delete_version(principal, ARTIFACT_REF, 9),
        lambda: service.delete_version(principal, ARTIFACT_REF, True),
        lambda: service.delete_version(principal, _OTHER, 1),
    ):
        with pytest.raises((ConflictError, NotFoundError)):
            call()
    assert len(session.scalars(select(AuditEvent.id)).all()) == before


def test_a_version_that_cannot_be_one_is_refused_with_a_bounded_message(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    for bad in (True, 1.0, "1", None, 0, -1, 10**5000):
        with pytest.raises(NotFoundError) as caught:
            service.delete_version(principal, ARTIFACT_REF, bad)  # ty: ignore[invalid-argument-type]
        assert str(caught.value) == (
            "models.churn has no version with that number; versions count up from 1."
        )
    assert _read(service, principal, 1, b"one") == b"one"


def test_a_table_is_not_an_artifact_to_delete(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    ensure_table(session, "dev", "models", "plain")
    plain = TableRef("dev", ARTIFACT_REF.namespace, "plain")
    for call in (
        lambda: service.delete_version(principal, plain, 1),
        lambda: service.drop_artifact(principal, plain),
    ):
        with pytest.raises(ValidationError, match="is a table, not an artifact"):
            call()
    assert session.scalars(select(Table).where(Table.name == "plain")).one() is not None


def test_dropping_an_artifact_removes_rows_grants_and_bytes(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    ensure_table(session, "dev", "silver", "rows")
    store_version(service, principal, ARTIFACT_REF, _ONE, sources=[ArtifactSource("silver.rows")])
    store_version(service, principal, ARTIFACT_REF, _TWO)
    service.begin_upload(principal, ARTIFACT_REF, artifact_document(_THREE))
    store_version(service, principal, _OTHER, _ONE)
    other_before = _stored(session, "other")
    artifact = session.scalars(select(Table).where(Table.name == "churn")).one()
    artifact_id, environment_id = artifact.id, session.scalars(select(Environment.id)).one()
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=principal.id,
            role=Role.VIEWER,
            kind=SecurableKind.TABLE,
            environment_id=environment_id,
            environment="dev",
            namespace="models",
            table="churn",
            table_id=artifact_id,
            granted_by=principal.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()

    service.drop_artifact(principal, ARTIFACT_REF)

    storage = StorageResolver(session).for_environment("dev").storage
    assert list(storage.list(artifact_prefix(artifact_id))) == []
    assert _stored(session, "other") == other_before != set()
    assert _read_other(service, principal) == _ONE["model.json"]
    assert session.get(Table, artifact_id) is None
    assert {v.artifact_id for v in session.scalars(select(ArtifactVersion))} != {artifact_id}
    assert (
        session.scalars(
            select(ArtifactUpload).where(ArtifactUpload.artifact_id == artifact_id)
        ).all()
        == []
    )
    assert artifact_id not in set(session.scalars(select(ArtifactBlob.artifact_id)))
    assert session.scalars(select(Grant)).one().revoked_at is not None
    assert session.scalars(select(LineageEdge.source_name)).all() == ["silver.rows"]
    (dropped,) = _events(session, AuditEventType.ARTIFACT_DROPPED)
    assert dropped.detail == {
        "artifact": "models.churn",
        "artifact_id": str(artifact_id),
        "versions": 2,
        "principal": "admin",
    }
    with pytest.raises(
        NotFoundError, match=re.escape("Artifact 'models.churn' not found in 'dev'.")
    ):
        service.drop_artifact(principal, ARTIFACT_REF)


def _read_other(service: ArtifactService, p: PrincipalRef) -> bytes:
    """Download `models.other`'s `model.json`.

    Parameters
    ----------
    service
        The service.
    p
        The reader.

    Returns
    -------
    bytes
        The file.
    """
    return b"".join(service.open_blob(p, _OTHER, 1, _sha(b"one")).chunks)


def test_a_new_artifact_of_a_dropped_name_inherits_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point G: a recreated name is another artifact: new id, version 1, no old grant or edge."""
    service = artifact_service(session, tmp_path)
    ensure_table(session, "dev", "silver", "rows")
    store_version(service, principal, ARTIFACT_REF, _ONE, sources=[ArtifactSource("silver.rows")])
    store_version(service, principal, ARTIFACT_REF, _TWO)
    old_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=principal.id,
            role=Role.VIEWER,
            kind=SecurableKind.TABLE,
            environment_id=session.scalars(select(Environment.id)).one(),
            environment="dev",
            namespace="models",
            table="churn",
            table_id=old_id,
            granted_by=principal.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    service.drop_artifact(principal, ARTIFACT_REF)

    again = store_version(service, principal, ARTIFACT_REF, _ONE)
    new_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    assert new_id != old_id
    assert again.version.version == 1
    assert _stored(session) == {
        f"blobs/{_sha(b'one')}",
        f"blobs/{_sha(_SHARED)}",
        "manifests/1.json",
    }
    assert (
        list(StorageResolver(session).for_environment("dev").storage.list(artifact_prefix(old_id)))
        == []
    )
    (grant,) = session.scalars(select(Grant)).all()
    assert grant.revoked_at is not None
    assert grant.table_id != new_id
    assert not [g for g in session.scalars(select(Grant)) if g.table_id == new_id]
    edge = session.scalars(select(LineageEdge)).one()
    assert edge.target_table_id == old_id != new_id


def test_a_drop_only_ever_touches_the_artifacts_own_directory(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point G: a listing that returned a path outside the prefix would not be followed."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, _OTHER, _ONE)
    storage = StorageResolver(session).for_environment("dev").storage
    storage.write("managed/keep", b"table data")
    real = FsspecStorage.list

    def wide(self: FsspecStorage, prefix: str) -> Iterator[str]:
        yield from real(self, prefix)
        yield "managed/keep"
        yield from real(self, "artifacts")

    monkeypatch.setattr(FsspecStorage, "list", wide)
    service.drop_artifact(principal, ARTIFACT_REF)
    monkeypatch.undo()
    assert storage.read("managed/keep") == b"table data"
    assert _read_other(service, principal) == b"one"
    assert _stored(session, "other") != set()


def test_bytes_that_cannot_be_removed_are_left_for_the_sweep(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rows first, bytes second; a storage failure never undoes or blocks a deletion."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    actor = _producer(session, principal, 1)

    def broken(self: FsspecStorage, path: str) -> None:
        raise OSError("storage is down at /secret/path")

    monkeypatch.setattr(FsspecStorage, "delete", broken)
    with caplog.at_level(logging.WARNING):
        deleted = service.delete_version(principal, ARTIFACT_REF, 1)
        pruned = store_version(service, actor, ARTIFACT_REF, _THREE)
        service.drop_artifact(principal, ARTIFACT_REF)
    assert deleted.deleted_at is not None
    assert pruned.deleted_versions == (2,)
    assert session.scalars(select(Table).where(Table.name == "churn")).all() == []
    assert sum("the sweep removes them" in record.getMessage() for record in caplog.records) == 3
    assert all("storage is down" not in record.getMessage() for record in caplog.records)
    assert all("/secret" not in record.getMessage() for record in caplog.records)


def test_after_a_failed_storage_delete_the_same_file_uploads_again_cleanly(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point E: bytes without rows are garbage; the next upload stores them again."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)

    def broken(self: FsspecStorage, path: str) -> None:
        raise OSError("crash")

    with monkeypatch.context() as patched:
        patched.setattr(FsspecStorage, "delete", broken)
        service.delete_version(principal, ARTIFACT_REF, 1)
    assert f"blobs/{_sha(b'one')}" in _stored(session)
    assert session.scalars(select(ArtifactBlob)).all() == []

    again = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_ONE))
    assert set(again.missing) == {_sha(b"one"), _sha(_SHARED)}
    for data in (b"one", _SHARED):
        assert service.put_blob(principal, ARTIFACT_REF, again.upload_id, _sha(data), [data])
    assert service.commit_upload(principal, ARTIFACT_REF, again.upload_id).version.version == 2
    assert _read(service, principal, 2, b"one") == b"one"
    assert _read(service, principal, 2, _SHARED) == _SHARED


def test_a_file_stored_again_before_the_bytes_are_removed_is_kept(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point E: a row without bytes must never exist: the release re-checks under the lock."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)

    def uploaded_in_between() -> None:
        # The commit of the deletion is done; another request stores the same file now.
        upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"one"}))
        assert upload.missing == (_sha(b"one"),)
        service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"one"), [b"one"])
        service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)

    _before_lock(monkeypatch, uploaded_in_between, skip=1)
    service.delete_version(principal, ARTIFACT_REF, 1)
    monkeypatch.undo()
    assert _read(service, principal, 2, b"one") == b"one"
    assert _sha(b"one") in set(session.scalars(select(ArtifactBlob.sha256)))
    assert f"blobs/{_sha(b'one')}" in _stored(session)
    assert f"blobs/{_sha(_SHARED)}" not in _stored(session)


def test_delete_quietly_reports_whether_something_was_there(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    artifact_service(session, tmp_path)
    storage = StorageResolver(session).for_environment("dev").storage
    storage.write("x/y", b"1")
    assert delete_quietly(storage, "x/y") is True
    assert delete_quietly(storage, "x/y") is False


def test_a_drop_under_a_lost_lock_changes_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", lambda _s, _a: False)
    for call in (
        lambda: service.delete_version(principal, ARTIFACT_REF, 1),
        lambda: service.drop_artifact(principal, ARTIFACT_REF),
    ):
        with pytest.raises(
            NotFoundError, match=re.escape("Artifact 'models.churn' not found in 'dev'.")
        ):
            call()
    monkeypatch.undo()
    assert _read(service, principal, 1, b"one") == b"one"
    assert _events(session, AuditEventType.ARTIFACT_VERSION_DELETED) == []


def test_a_damaged_manifest_never_becomes_a_storage_path(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    storage = StorageResolver(session).for_environment("dev").storage
    storage.write("managed/keep", b"table data")
    session.execute(
        update(ArtifactVersion).values(manifest_json={"files": [{"sha256": "../../managed/keep"}]})
    )
    session.commit()
    assert service.delete_version(principal, ARTIFACT_REF, 1).deleted_at is not None
    assert storage.read("managed/keep") == b"table data"


def _damage(session: Session, version: int) -> None:
    """Make one version's stored manifest unreadable.

    Parameters
    ----------
    session
        Database session.
    version
        The version.
    """
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.version == version)
        .values(manifest_json={"files": "damaged"})
    )
    session.commit()


def test_a_damaged_manifest_of_a_live_version_stops_every_byte_being_deleted(
    session: Session, tmp_path: Path, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    """Point D: damage reads as "may name anything", never "names nothing"."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    _damage(session, 2)
    before = _stored(session)
    with caplog.at_level(logging.WARNING):
        deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert deleted.deleted_at is not None
    # The version is deleted; its manifest object goes, every blob stays, rows included.
    assert _stored(session) == before - {"manifests/1.json"}
    assert len(session.scalars(select(ArtifactBlob)).all()) == 3
    (event,) = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert event.detail["freed_files"] == 0
    assert "damaged" in str(event.detail["kept_reason"])
    assert "Delete the damaged version" in str(event.detail["kept_reason"])
    assert any("damaged" in record.getMessage() for record in caplog.records)


def test_a_damaged_manifest_of_an_open_upload_stops_every_byte_being_deleted(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"one"}))
    session.execute(
        update(ArtifactUpload)
        .where(ArtifactUpload.id == upload.upload_id)
        .values(manifest_json={"files": []})
    )
    session.commit()
    service.delete_version(principal, ARTIFACT_REF, 1)
    assert {f"blobs/{_sha(b'one')}", f"blobs/{_sha(_SHARED)}"} <= _stored(session)
    (event,) = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert event.detail["freed_files"] == 0
    assert "kept_reason" in event.detail


def test_a_damaged_manifest_of_the_version_being_deleted_frees_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    _damage(session, 1)
    service.delete_version(principal, ARTIFACT_REF, 1)
    assert f"blobs/{_sha(b'one')}" in _stored(session)
    (event,) = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert (event.detail["freed_files"], "kept_reason" in event.detail) == (0, True)
    assert _read(service, principal, 2, b"two") == b"two"


def test_pruning_with_a_damaged_manifest_deletes_no_bytes(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    _damage(session, 2)
    before = _stored(session)
    actor = _producer(session, principal, 1)
    result = store_version(service, actor, ARTIFACT_REF, _THREE)
    assert result.deleted_versions == (2, 1)
    # Version 2 is retired too and its own manifest is damaged, so its files are kept; version 1
    # is sound and the new live version does not need its file.
    assert f"blobs/{_sha(b'two')}" in _stored(session)
    assert f"blobs/{_sha(b'one')}" not in _stored(session)
    assert _stored(session) - before == {f"blobs/{_sha(b'three')}", "manifests/3.json"}
    assert _read(service, principal, 3, b"three") == b"three"


def test_the_referenced_set_is_built_from_a_stream_of_manifests(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point D: no query loads every manifest of the artifact at once."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    seen: list[object] = []

    def watch(state: ORMExecuteState) -> None:
        text = str(state.statement)
        if "manifest_json" in text and "artifact_versions.deleted_at IS NULL" in text:
            seen.append(state.execution_options.get("yield_per"))

    event.listen(session, "do_orm_execute", watch)
    try:
        service.delete_version(principal, ARTIFACT_REF, 1)
    finally:
        event.remove(session, "do_orm_execute", watch)
    assert seen
    assert all(isinstance(rows, int) and 0 < rows <= 8 for rows in seen)


def test_the_database_decides_not_a_manifest_object(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point D: a stored manifest object, orphan or tampered, is never trusted."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    storage = StorageResolver(session).for_environment("dev").storage
    # Version 2's object now claims it needs nothing; an orphan of a number never committed
    # claims to need version 1's file.
    storage.write(manifest_path(artifact_id, 2), b'{"files":[]}')
    storage.write(
        manifest_path(artifact_id, 7), b'{"files":[{"sha256":"%s"}]}' % _sha(b"one").encode()
    )
    service.delete_version(principal, ARTIFACT_REF, 1)
    assert _read(service, principal, 2, b"two") == b"two"
    assert _read(service, principal, 2, _SHARED) == _SHARED
    # The object of the number never committed names version 1's file, so the request frees
    # nothing (it cannot know whether that is a version a restore forgot); the sweep decides.
    assert f"blobs/{_sha(b'one')}" in _stored(session)
    assert "manifests/7.json" in _stored(session)
    service.drop_artifact(principal, ARTIFACT_REF)
    assert list(storage.list(artifact_prefix(artifact_id))) == []


def test_two_deletes_of_one_version_delete_it_once(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point F: the second request, waiting for the lock, finds the version deleted."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    _before_lock(monkeypatch, lambda: service.delete_version(principal, ARTIFACT_REF, 1))
    with pytest.raises(ConflictError, match=r"Version 1 of models.churn is already deleted."):
        service.delete_version(principal, ARTIFACT_REF, 1)
    monkeypatch.undo()
    assert len(_events(session, AuditEventType.ARTIFACT_VERSION_DELETED)) == 1
    assert _read(service, principal, 2, b"two") == b"two"
    assert _read(service, principal, 2, _SHARED) == _SHARED


def test_a_delete_between_the_lookup_and_the_lock_of_a_commit_loses_no_file(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point F: an upload begun before the delete commits after it; its files are there."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    upload = service.begin_upload(
        principal, ARTIFACT_REF, artifact_document({"m": b"one", "w": _SHARED})
    )
    assert upload.missing == ()
    _before_lock(monkeypatch, lambda: service.delete_version(principal, ARTIFACT_REF, 1))
    committed = service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    monkeypatch.undo()
    assert committed.version.version == 2
    assert _read(service, principal, 2, b"one") == b"one"
    assert _read(service, principal, 2, _SHARED) == _SHARED


def test_a_commit_after_a_delete_that_freed_its_file_reports_it_missing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point F: if the file is gone at commit, the commit fails cleanly and names the count."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    now = datetime.now(UTC)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"one"}))
    # Its upload expired (the worker's sweep or a clock): the file is no longer protected.
    session.execute(update(ArtifactUpload).values(expires_at=now - timedelta(seconds=1)))
    session.commit()
    service.delete_version(principal, ARTIFACT_REF, 1)
    assert f"blobs/{_sha(b'one')}" not in _stored(session)
    with pytest.raises(ConflictError, match="This upload has expired or does not exist"):
        service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    fresh = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"one"}))
    assert fresh.missing == (_sha(b"one"),)
    assert (
        session.scalars(select(ArtifactVersion).where(ArtifactVersion.deleted_at.is_(None))).all()
        == []
    )


def test_a_download_in_progress_when_its_version_is_deleted_is_whole_or_fails_cleanly(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    stream = service.open_blob(principal, ARTIFACT_REF, 1, _sha(b"one"))
    service.delete_version(principal, ARTIFACT_REF, 1)
    try:
        body = b"".join(stream.chunks)
    except (NotFoundError, OSError):
        body = b"one"
    assert body == b"one"
    again = store_version(service, principal, ARTIFACT_REF, _ONE)
    assert _read(service, principal, again.version.version, b"one") == b"one"


def test_a_drop_while_an_upload_is_open_leaves_nothing_and_the_upload_fails_cleanly(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point F: a file being received when the artifact is dropped is neither kept nor visible."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"new"}))
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    blob = service.receive_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"new"))
    blob.write(b"new")
    service.drop_artifact(principal, ARTIFACT_REF)
    with pytest.raises(ConflictError, match="This upload has expired or does not exist"):
        blob.finish()
    storage = StorageResolver(session).for_environment("dev").storage
    assert list(storage.list(artifact_prefix(artifact_id))) == []
    for call in (
        lambda: service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"new"), [b"new"]),
        lambda: service.commit_upload(principal, ARTIFACT_REF, upload.upload_id),
    ):
        with pytest.raises(NotFoundError, match="not found"):
            call()


def test_a_drop_that_waits_for_a_commit_removes_the_version_it_made(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point F: commit first, then drop: the new version and its bytes go with the artifact."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_TWO))
    for data in (b"two",):
        service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(data), [data])
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    _before_lock(
        monkeypatch, lambda: service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    )
    service.drop_artifact(principal, ARTIFACT_REF)
    monkeypatch.undo()
    (dropped,) = _events(session, AuditEventType.ARTIFACT_DROPPED)
    assert dropped.detail["versions"] == 2
    storage = StorageResolver(session).for_environment("dev").storage
    assert list(storage.list(artifact_prefix(artifact_id))) == []
    assert session.scalars(select(ArtifactVersion)).all() == []


def test_a_commit_that_waits_for_a_drop_is_reported_as_gone(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"one"}))
    _before_lock(monkeypatch, lambda: service.drop_artifact(principal, ARTIFACT_REF))
    with pytest.raises(
        NotFoundError, match=re.escape("Artifact 'models.churn' not found in 'dev'.")
    ):
        service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    monkeypatch.undo()
    assert session.scalars(select(ArtifactVersion)).all() == []
    assert session.scalars(select(Table).where(Table.name == "churn")).all() == []


def test_a_name_recreated_while_a_delete_waits_is_not_touched(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete holds the id it looked up; a new artifact of that name has another."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)

    def drop_and_recreate() -> None:
        service.drop_artifact(principal, ARTIFACT_REF)
        store_version(service, principal, ARTIFACT_REF, _ONE)

    _before_lock(monkeypatch, drop_and_recreate)
    with pytest.raises(
        NotFoundError, match=re.escape("Artifact 'models.churn' not found in 'dev'.")
    ):
        service.delete_version(principal, ARTIFACT_REF, 1)
    monkeypatch.undo()
    assert _read(service, principal, 1, b"one") == b"one"


def test_a_version_row_cached_before_the_lock_is_read_fresh_after_it(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point F: another transaction's delete, seen only through the lock, is not repeated."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)

    cached: list[ArtifactVersion] = []  # the identity map holds its rows weakly

    def deleted_elsewhere() -> None:
        row = session.scalars(select(ArtifactVersion).where(ArtifactVersion.version == 1)).one()
        assert row.deleted_at is None
        cached.append(row)
        session.execute(
            update(ArtifactVersion)
            .where(ArtifactVersion.version == 1)
            .values(deleted_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )

    _before_lock(monkeypatch, deleted_elsewhere)
    with pytest.raises(ConflictError, match=r"Version 1 of models.churn is already deleted."):
        service.delete_version(principal, ARTIFACT_REF, 1)
    monkeypatch.undo()
    assert _events(session, AuditEventType.ARTIFACT_VERSION_DELETED) == []


def test_an_artifact_dropped_before_the_bytes_are_released_ends_the_release_quietly(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    _before_lock(monkeypatch, lambda: service.drop_artifact(principal, ARTIFACT_REF), skip=1)
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    monkeypatch.undo()
    assert deleted.deleted_at is not None
    storage = StorageResolver(session).for_environment("dev").storage
    assert list(storage.list(artifact_prefix(artifact_id))) == []


def test_many_files_are_released_one_lock_at_a_time(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    files = {f"f{i}": f"file {i}".encode() for i in range(5)}
    store_version(service, principal, ARTIFACT_REF, files)
    locks: list[int] = []
    real = lock_artifact
    monkeypatch.setattr(
        "lakana_core.artifacts.service.lock_artifact",
        lambda s, a: (locks.append(1), real(s, a))[1],
    )
    service.delete_version(principal, ARTIFACT_REF, 1)
    assert _stored(session) == set()
    assert session.scalars(select(ArtifactBlob)).all() == []
    assert len(locks) == 1 + 5  # the deletion's own, then one per file


def test_a_file_stored_again_mid_release_is_kept(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is checked for each file right before its delete, not once for a batch."""
    service = artifact_service(session, tmp_path)
    files = {f"f{i}": f"file {i}".encode() for i in range(3)}
    store_version(service, principal, ARTIFACT_REF, files)
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    order = sorted(_sha(data) for data in files.values())
    second = order[1]
    real = FsspecStorage.delete
    state = {"calls": 0}

    def delete_and_store_the_second_again(self: FsspecStorage, path: str) -> None:
        state["calls"] += 1
        if state["calls"] == 1:
            # What `finish` of another upload leaves: the row (its bytes are still there).
            session.add(
                ArtifactBlob(
                    artifact_id=artifact_id,
                    sha256=second,
                    size_bytes=7,
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()
        real(self, path)

    monkeypatch.setattr(FsspecStorage, "delete", delete_and_store_the_second_again)
    service.delete_version(principal, ARTIFACT_REF, 1)
    monkeypatch.undo()
    assert f"blobs/{second}" in _stored(session)
    assert {n for n in _stored(session) if n.startswith("blobs/")} == {f"blobs/{second}"}


def test_a_delete_whose_commit_fails_touches_no_byte(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows first, bytes second: a commit that fails leaves every byte of a live version."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    before = _stored(session)

    def boom() -> None:
        raise RuntimeError("the commit's last check failed")

    monkeypatch.setattr(service._guard, "record_pending", boom)
    with pytest.raises(RuntimeError, match="last check"):
        service.delete_version(principal, ARTIFACT_REF, 1)
    monkeypatch.undo()
    assert _stored(session) == before
    assert session.scalars(select(ArtifactVersion.deleted_at)).one() is None
    assert len(session.scalars(select(ArtifactBlob)).all()) == 2
    assert _read(service, principal, 1, b"one") == b"one"
    assert _read(service, principal, 1, _SHARED) == _SHARED
    assert _events(session, AuditEventType.ARTIFACT_VERSION_DELETED) == []


def test_a_pruning_commit_that_fails_touches_no_byte(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    before = _stored(session)
    actor = _producer(session, principal, 1)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_THREE))
    service.put_blob(actor, ARTIFACT_REF, upload.upload_id, _sha(b"three"), [b"three"])
    real = FsspecStorage.open_write

    def refuse_manifests(self: FsspecStorage, path: str) -> StreamWriter:
        if "/manifests/" in path:
            raise OSError("no space left")
        return real(self, path)

    monkeypatch.setattr(FsspecStorage, "open_write", refuse_manifests)
    with pytest.raises(
        ConflictError, match="not committed because its storage could not be written"
    ):
        service.commit_upload(actor, ARTIFACT_REF, upload.upload_id)
    monkeypatch.undo()
    assert before <= _stored(session)
    assert [v.deleted_at for v in session.scalars(select(ArtifactVersion))] == [None, None]
    for number, files in ((1, _ONE), (2, _TWO)):
        for data in files.values():
            assert _read(service, principal, number, data) == data
    assert _events(session, AuditEventType.ARTIFACT_VERSION_DELETED) == []


def test_a_task_token_of_any_form_never_deletes(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Even a token minted for a principal, not bound to a run, cannot delete (allow-all authz)."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    unbound = dataclasses.replace(
        principal, task_scope=TaskScope("dev", ["models.*"], uuid.uuid4(), None)
    )
    for call in (
        lambda: service.delete_version(unbound, ARTIFACT_REF, 1),
        lambda: service.drop_artifact(unbound, ARTIFACT_REF),
    ):
        with pytest.raises(PermissionDenied) as caught:
            call()
        assert str(caught.value) == DELETE_FROM_RUN_MESSAGE
    assert _read(service, principal, 1, b"one") == b"one"


def test_the_guard_refuses_before_anything_is_looked_up(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A protected environment answers "protected" for a missing artifact or version too."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=True))
    session.commit()
    ghost = TableRef("dev", ARTIFACT_REF.namespace, "ghost")
    calls = (
        lambda: service.delete_version(principal, ghost, 1),
        lambda: service.delete_version(principal, ARTIFACT_REF, 9),
        lambda: service.drop_artifact(principal, ghost),
    )
    for call in calls:
        before = len(_events(session, AuditEventType.PROTECTED_WRITE_DENIED))
        with pytest.raises(PermissionDenied, match="is protected"):
            call()
        assert len(_events(session, AuditEventType.PROTECTED_WRITE_DENIED)) == before + 1


def test_each_destructive_operation_records_its_own_break_glass_event(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    for files in (_ONE, _TWO):
        store_version(service, principal, ARTIFACT_REF, files)
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=True))
    session.commit()
    glass = dataclasses.replace(principal, break_glass_reason="remove a leaked training set")
    ghost = TableRef("dev", ARTIFACT_REF.namespace, "ghost")
    # Refused and not-found operations record nothing and leave nothing pending.
    with pytest.raises(NotFoundError):
        service.drop_artifact(glass, ghost)
    with pytest.raises(NotFoundError):
        service.delete_version(glass, ARTIFACT_REF, 9)
    assert _events(session, AuditEventType.BREAK_GLASS_WRITE) == []
    service.delete_version(glass, ARTIFACT_REF, 1)
    service.delete_version(glass, ARTIFACT_REF, 2)
    service.drop_artifact(glass, ARTIFACT_REF)
    operations = [e.detail["operation"] for e in _events(session, AuditEventType.BREAK_GLASS_WRITE)]
    assert operations == ["delete_artifact_version", "delete_artifact_version", "drop_artifact"]


def test_a_file_two_pruned_versions_share_is_counted_once(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "x": b"x"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b", "x": b"x"})
    actor = _producer(session, principal, 1)
    assert store_version(service, actor, ARTIFACT_REF, {"c": b"c"}).deleted_versions == (2, 1)
    counts = {
        e.detail["version"]: e.detail["freed_files"]
        for e in _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    }
    assert counts == {2: 2, 1: 1}
    assert _stored(session) == {f"blobs/{_sha(b'c')}", "manifests/3.json"}


def test_a_pruning_event_names_the_task_and_a_requested_one_does_not(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    actor = _producer(session, principal, 1)
    store_version(service, actor, ARTIFACT_REF, _TWO)
    assert actor.run_context is not None
    (pruned,) = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert pruned.detail["task_run_id"] == str(actor.run_context.task_run_id)
    added = [
        e
        for e in _events(session, AuditEventType.ARTIFACT_VERSION_ADDED)
        if e.detail["version"] == 2
    ]
    assert added[0].detail["task_run_id"] == pruned.detail["task_run_id"]
    service.delete_version(principal, ARTIFACT_REF, 2)
    requested = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)[-1]
    assert "task_run_id" not in requested.detail


def test_a_drop_lists_the_artifacts_directory_lazily(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Objects are deleted as the listing yields them; none is held back for a second pass."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    real = FsspecStorage.list
    trace: list[str] = []

    def traced(self: FsspecStorage, prefix: str) -> Iterator[str]:
        for path in real(self, prefix):
            trace.append("yield")
            yield path

    real_delete = FsspecStorage.delete

    def delete(self: FsspecStorage, path: str) -> None:
        trace.append("delete")
        real_delete(self, path)

    monkeypatch.setattr(FsspecStorage, "list", traced)
    monkeypatch.setattr(FsspecStorage, "delete", delete)
    service.drop_artifact(principal, ARTIFACT_REF)
    monkeypatch.undo()
    # Three objects and the marker are yielded; the marker is deleted last, after the loop.
    assert trace.count("yield") == 4
    assert trace.count("delete") == 4
    assert trace.index("delete") < len(trace) - 1 - trace[::-1].index("yield")
    assert trace[-1] == "delete"
    assert (
        list(
            StorageResolver(session)
            .for_environment("dev")
            .storage.list(artifact_prefix(artifact_id))
        )
        == []
    )


def test_a_refused_operation_does_not_leak_its_pending_record_into_the_next(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=True))
    session.commit()
    glass = dataclasses.replace(principal, break_glass_reason="remove a leaked training set")
    with pytest.raises(NotFoundError):
        service.delete_version(glass, ARTIFACT_REF, 9)
    service.drop_artifact(glass, ARTIFACT_REF)
    (event,) = _events(session, AuditEventType.BREAK_GLASS_WRITE)
    assert event.detail["operation"] == "drop_artifact"


def _protect(session: Session) -> None:
    """Make `dev` a protected environment.

    Parameters
    ----------
    session
        Database session.
    """
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=True))
    session.commit()


def _glass_operations(session: Session) -> list[str]:
    """List the operations of the break-glass events recorded so far, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of str
        The operation of each event.
    """
    return [str(e.detail["operation"]) for e in _events(session, AuditEventType.BREAK_GLASS_WRITE)]


def test_a_missed_delete_leaves_no_record_for_the_upload_that_follows(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    _protect(session)
    glass = dataclasses.replace(principal, break_glass_reason="repair by hand")
    fresh = ArtifactService(session, StorageResolver(session), AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        fresh.delete_version(glass, ARTIFACT_REF, 9)
    store_version(fresh, glass, ARTIFACT_REF, _TWO)
    assert _glass_operations(session) == ["artifact_version"]


def test_an_upload_under_break_glass_does_not_hide_the_deletion_after_it(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    _protect(session)
    glass = dataclasses.replace(principal, break_glass_reason="repair by hand")
    fresh = ArtifactService(session, StorageResolver(session), AllowAllAuthorization())
    store_version(fresh, glass, ARTIFACT_REF, _TWO)
    fresh.delete_version(glass, ARTIFACT_REF, 1)
    assert _glass_operations(session) == ["artifact_version", "delete_artifact_version"]


def test_a_pending_record_never_outlives_the_deletion_that_made_it(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A missed break-glass delete, then an ordinary one on the same target: no event."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    _protect(session)
    glass = dataclasses.replace(principal, break_glass_reason="repair by hand")
    fresh = ArtifactService(session, StorageResolver(session), AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        fresh.delete_version(glass, ARTIFACT_REF, 9)
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=False))
    session.commit()
    fresh.delete_version(principal, ARTIFACT_REF, 1)
    assert _glass_operations(session) == []


def test_the_reference_check_reads_past_the_first_batch_of_manifests(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Every file a later version names is found, however many versions precede it."""
    service = artifact_service(session, tmp_path)
    count = 2 * _STREAM_ROWS + 1
    names = [f"file {i}".encode() for i in range(count)]
    store_version(service, principal, ARTIFACT_REF, {f"f{i}": data for i, data in enumerate(names)})
    for i, data in enumerate(names):
        store_version(service, principal, ARTIFACT_REF, {"f": data, "u": f"unique {i}".encode()})
    service.delete_version(principal, ARTIFACT_REF, 1)
    stored = _stored(session)
    for data in names:
        assert f"blobs/{_sha(data)}" in stored
    for number, data in enumerate(names, start=2):
        assert _read(service, principal, number, data) == data
    (event,) = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert event.detail["freed_files"] == 0


def test_a_release_ends_when_the_artifact_is_gone(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(
        service, principal, ARTIFACT_REF, {f"f{i}": f"file {i}".encode() for i in range(3)}
    )
    state = {"in_hook": False, "deletes": 0, "locks": 0}
    real_delete, real_lock = FsspecStorage.delete, lock_artifact

    def delete(self: FsspecStorage, path: str) -> None:
        if state["in_hook"]:
            real_delete(self, path)
            return
        state["deletes"] += 1
        if state["deletes"] == 1:
            state["in_hook"] = True
            service.drop_artifact(principal, ARTIFACT_REF)
            state["in_hook"] = False
        real_delete(self, path)

    def lock(s: Session, artifact_id: uuid.UUID) -> bool:
        if not state["in_hook"]:
            state["locks"] += 1
        return real_lock(s, artifact_id)

    monkeypatch.setattr(FsspecStorage, "delete", delete)
    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", lock)
    service.delete_version(principal, ARTIFACT_REF, 1)
    monkeypatch.undo()
    # The deletion's own lock, the first file's, and the second file's, which failed.
    assert state["locks"] == 3
    assert state["deletes"] == 1


def test_the_lock_is_let_go_after_each_file(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(
        service, principal, ARTIFACT_REF, {f"f{i}": f"file {i}".encode() for i in range(3)}
    )
    rollbacks = [0]
    real_rollback, real_delete = session.rollback, FsspecStorage.delete
    seen: list[int] = []
    monkeypatch.setattr(
        session,
        "rollback",
        lambda: (rollbacks.__setitem__(0, rollbacks[0] + 1), real_rollback())[1],
    )

    def delete(self: FsspecStorage, path: str) -> None:
        seen.append(rollbacks[0])
        real_delete(self, path)

    monkeypatch.setattr(FsspecStorage, "delete", delete)
    service.delete_version(principal, ARTIFACT_REF, 1)
    monkeypatch.undo()
    # Right before the n-th delete the lock of the previous file has been let go.
    assert seen == [0, 1, 2, 3]


def test_a_release_stops_when_its_time_is_spent_and_the_version_is_gone_for_readers(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A request does not spend a client's timeout deleting files one by one."""
    setup = artifact_service(session, tmp_path)
    files = {f"f{i}": f"file {i}".encode() for i in range(6)}
    store_version(setup, principal, ARTIFACT_REF, files)
    # Finite, and an extra reading is recorded where `_release`'s `except Exception` cannot hide it.
    readings = [0.0, 2.5, 5.0]
    overrun: list[str] = []

    def clock() -> float:
        if readings:
            return readings.pop(0)
        overrun.append("the clock was read after the budget should have stopped the release")
        return 1e9

    service = ArtifactService(
        session, StorageResolver(session), AllowAllAuthorization(), monotonic=clock
    )
    service.delete_version(principal, ARTIFACT_REF, 1)

    left = {n for n in _stored(session) if n.startswith("blobs/")}
    # Started at 0; the checks read 2.5 (one file goes) and 5.0, which is exactly the budget.
    assert len(left) == 5
    assert overrun == []
    assert "manifests/1.json" in _stored(session)
    assert session.scalars(select(ArtifactBlob)).all() == []
    assert service.get_version(principal, ARTIFACT_REF, 1).files == ()
    with pytest.raises(NotFoundError, match="was deleted"):
        service.open_blob(principal, ARTIFACT_REF, 1, _sha(b"file 0"))


def test_a_release_that_finishes_within_its_budget_removes_the_manifest_object(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    service.delete_version(principal, ARTIFACT_REF, 1)
    assert _stored(session) == set()


def test_a_pruning_release_removes_the_manifest_of_a_version_it_finished(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Versions are released in order; a finished one loses its manifest, the rest keep it."""
    monkeypatch.setattr(
        "lakana_core.artifacts.service.RELEASE_BUDGET_FILES", 3
    )  # 2 files + 1 manifest
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a1", "b": b"b1"})
    store_version(service, principal, ARTIFACT_REF, {"a": b"a2", "b": b"b2"})
    store_version(service, principal, ARTIFACT_REF, {"a": b"a3", "b": b"b3"})
    # What `_retire` hands over for versions 2 and 1, newest first, rows already removed.
    retired = [(2, {_sha(b"a2"), _sha(b"b2")}), (1, {_sha(b"a1"), _sha(b"b1")})]
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    session.execute(
        delete(ArtifactBlob).where(
            ArtifactBlob.sha256.in_([_sha(d) for d in (b"a1", b"b1", b"a2", b"b2")])
        )
    )
    session.commit()
    service._release(ARTIFACT_REF, artifact_id, retired)
    remaining = _stored(session)
    assert "manifests/2.json" not in remaining
    assert "manifests/1.json" in remaining
    assert {n for n in remaining if n.startswith("blobs/")} == {
        f"blobs/{_sha(b'a1')}",
        f"blobs/{_sha(b'b1')}",
        f"blobs/{_sha(b'a3')}",
        f"blobs/{_sha(b'b3')}",
    }


def _forget(session: Session, *versions: int) -> None:
    """Make the database forget versions and their file rows, as a restore does.

    Parameters
    ----------
    session
        Database session.
    *versions
        The numbers; their manifest objects and bytes stay.
    """
    hashes: set[str] = set()
    for row in session.scalars(
        select(ArtifactVersion).where(ArtifactVersion.version.in_(versions))
    ):
        hashes |= {entry["sha256"] for entry in row.manifest_json["files"]}
    session.execute(delete(ArtifactVersion).where(ArtifactVersion.version.in_(versions)))
    session.execute(delete(ArtifactBlob).where(ArtifactBlob.sha256.in_(hashes)))
    session.commit()


def test_a_deletion_frees_nothing_while_storage_holds_versions_the_catalog_forgot(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Probe b: version 1 shares a file with versions 2 and 3, which a restore forgot."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "s": b"s"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b", "s": b"s"})
    store_version(service, principal, ARTIFACT_REF, {"c": b"c", "s": b"s"})
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    _forget(session, 2, 3)

    service.delete_version(principal, ARTIFACT_REF, 1)

    stored = _stored(session)
    assert f"blobs/{_sha(b's')}" in stored  # shared with a forgotten version
    assert f"blobs/{_sha(b'a')}" in stored  # nothing is freed in the request
    (event,) = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert event.detail["kept_reason"] == KEPT_UNKNOWN_MANIFESTS
    assert event.detail["freed_files"] == 0
    # The sweep decides with the manifests it can read: `a` is named by nothing, `s` is not.
    sweep_artifacts(
        session,
        StorageResolver(session),
        now=datetime.now(UTC) + timedelta(days=9),
        upload_ttl_seconds=3600,
    )
    stored = _stored(session)
    assert f"blobs/{_sha(b's')}" in stored
    assert f"blobs/{_sha(b'b')}" in stored and f"blobs/{_sha(b'c')}" in stored
    assert f"blobs/{_sha(b'a')}" not in stored
    assert artifact_id is not None


def test_pruning_frees_nothing_while_storage_holds_versions_the_catalog_forgot(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    StorageResolver(session).for_environment("dev").storage.write(
        manifest_path(artifact_id, 9), b'{"files":[{"sha256":"%s"}]}' % _sha(b"one").encode()
    )
    actor = _producer(session, principal, 1)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_THREE))
    service.put_blob(actor, ARTIFACT_REF, upload.upload_id, _sha(b"three"), [b"three"])
    result = service.commit_upload(actor, ARTIFACT_REF, upload.upload_id)
    assert result.version.version == 10  # above the object of the number no row has
    assert result.deleted_versions == (2, 1)
    assert f"blobs/{_sha(b'one')}" in _stored(session)
    events = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert {e.detail["kept_reason"] for e in events} == {KEPT_UNKNOWN_MANIFESTS}


def test_a_deletion_works_while_storage_cannot_be_listed_and_frees_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-1: a failed listing of `manifests/` counts as "unknown manifests exist"."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)

    def down(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        raise OSError("/secret/bucket/path: connection refused")

    monkeypatch.setattr(FsspecStorage, "list_objects", down)
    view = service.delete_version(principal, ARTIFACT_REF, 1)
    assert view.deleted_at is not None
    monkeypatch.undo()
    stored = _stored(session)
    assert f"blobs/{_sha(b'one')}" in stored  # nothing was freed: the sweep decides later
    (event,) = _events(session, AuditEventType.ARTIFACT_VERSION_DELETED)
    assert event.detail["kept_reason"] == KEPT_UNKNOWN_MANIFESTS
    assert event.detail["freed_files"] == 0
    assert "secret" not in str(event.detail)
