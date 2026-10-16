"""A caller lists their own open uploads, so a killed upload can be withdrawn."""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
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
from sqlalchemy import event
from sqlalchemy.orm import Session

from lakana_core.artifacts.service import ArtifactService, UploadCursor
from lakana_core.auth import Action, BoundTask, PrincipalRef, RunContext, TaskScope
from lakana_core.config import ArtifactsConfig
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.models import Principal, PrincipalType
from lakana_core.storage.resolver import StorageResolver

_FILES = {"m": b"abc"}
_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _other(session: Session, name: str = "other") -> PrincipalRef:
    """Persist another person.

    Parameters
    ----------
    session
        Database session.
    name
        The person's name.

    Returns
    -------
    PrincipalRef
        The other caller.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name, is_active=True)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _attempt(session: Session, principal: PrincipalRef, claim: int = 1) -> PrincipalRef:
    """Make the principal of a running attempt of the task that produces `models.churn`.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    claim
        The claim the token carries.

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
        claim=claim,
        extra={"output": "artifact"},
    )
    bound = BoundTask(
        task.id, claim, "models.churn", ("silver.rows",), ("silver.rows",), "artifact"
    )
    return dataclasses.replace(
        principal,
        task_scope=TaskScope("dev", [], uuid.uuid4(), bound),
        run_context=RunContext(task_run_id=task.id, claim=claim),
    )


def test_a_caller_lists_their_open_uploads_newest_first(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    now = [_NOW]
    service = artifact_service(session, tmp_path, clock=lambda: now[0])
    first = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    now[0] += timedelta(minutes=5)
    elsewhere = dataclasses.replace(ARTIFACT_REF, name="other_model")
    second = service.begin_upload(principal, elsewhere, artifact_document(_FILES))
    page = service.list_uploads(principal, "dev")
    assert [(u.upload_id, u.ref.dotted()) for u in page.items] == [
        (second.upload_id, "models.other_model"),
        (first.upload_id, "models.churn"),
    ]
    assert page.items[1].created_at == _NOW
    assert page.items[1].expires_at == first.expires_at
    assert page.next is None


def test_only_the_callers_own_open_uploads_are_listed(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    now = [_NOW]
    service = artifact_service(
        session, tmp_path, config=ArtifactsConfig(upload_ttl_seconds=60), clock=lambda: now[0]
    )
    other = _other(session)
    committed = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"c": b"c"}))
    service.put_blob(principal, ARTIFACT_REF, committed.upload_id, _sha(b"c"), [b"c"])
    service.commit_upload(principal, ARTIFACT_REF, committed.upload_id)
    expired = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"e": b"e"}))
    now[0] += timedelta(seconds=61)
    theirs = service.begin_upload(other, ARTIFACT_REF, artifact_document(_FILES))
    mine = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    assert [u.upload_id for u in service.list_uploads(principal, "dev").items] == [mine.upload_id]
    assert [u.upload_id for u in service.list_uploads(other, "dev").items] == [theirs.upload_id]
    assert expired.upload_id not in {
        u.upload_id for u in service.list_uploads(principal, "dev").items
    }


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


def test_a_name_narrows_the_list_to_one_artifact(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    wanted = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    elsewhere = dataclasses.replace(ARTIFACT_REF, name="other_model")
    service.begin_upload(principal, elsewhere, artifact_document(_FILES))
    page = service.list_uploads(principal, "dev", name="models.churn")
    assert [u.upload_id for u in page.items] == [wanted.upload_id]
    assert service.list_uploads(principal, "dev", name="models.nothing").items == ()


def test_a_name_that_is_not_dotted_is_refused_with_the_next_step(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    with pytest.raises(ValidationError):
        service.list_uploads(principal, "dev", name="no_dot")


def test_a_list_pages_by_a_cursor_without_skipping_or_repeating(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    now = [_NOW]
    service = artifact_service(session, tmp_path, clock=lambda: now[0])
    made = []
    for _ in range(5):
        made.append(service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES)))
        now[0] += timedelta(seconds=1)
    seen: list[uuid.UUID] = []
    after: UploadCursor | None = None
    pages = 0
    while True:
        page = service.list_uploads(principal, "dev", limit=2, after=after)
        pages += 1
        assert len(page.items) <= 2
        seen += [u.upload_id for u in page.items]
        if page.next is None:
            break
        after = page.next
    assert (pages, seen) == (3, [u.upload_id for u in reversed(made)])


def test_uploads_begun_in_the_same_instant_still_page_cleanly(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path, clock=lambda: _NOW)
    made = {
        service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES)).upload_id
        for _ in range(4)
    }
    first = service.list_uploads(principal, "dev", limit=3)
    assert first.next is not None
    second = service.list_uploads(principal, "dev", limit=3, after=first.next)
    assert {u.upload_id for u in (*first.items, *second.items)} == made
    assert len(first.items) + len(second.items) == 4


def test_an_attempt_sees_the_uploads_of_its_own_attempt_only(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    person_upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    actor = _attempt(session, principal)
    mine = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES))
    assert actor.run_context is not None
    later_claim = dataclasses.replace(
        actor, run_context=RunContext(actor.run_context.task_run_id, claim=2)
    )
    assert [u.upload_id for u in service.list_uploads(actor, "dev").items] == [mine.upload_id]
    assert service.list_uploads(later_claim, "dev").items == ()
    outside = dataclasses.replace(actor, task_scope=None, run_context=None)
    assert [u.upload_id for u in service.list_uploads(outside, "dev").items] == [
        person_upload.upload_id
    ]


def test_a_bound_token_without_its_attempt_is_refused(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    actor = _attempt(session, principal)
    headless = dataclasses.replace(actor, run_context=None)
    with pytest.raises(PermissionDenied):
        service.list_uploads(headless, "dev")


def test_the_environment_must_be_readable_and_must_exist(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    blind = ArtifactService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))
    with pytest.raises(PermissionDenied, match="may not READ_ENVIRONMENT"):
        blind.list_uploads(principal, "dev")
    with pytest.raises(NotFoundError, match="No environment named 'nowhere'"):
        service.list_uploads(principal, "nowhere")


def test_an_upload_in_an_artifact_the_caller_may_no_longer_write_is_not_listed(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """They could not withdraw it, so the list does not offer it."""
    artifact_service(session, tmp_path).begin_upload(
        principal, ARTIFACT_REF, artifact_document(_FILES)
    )
    reader = ArtifactService(
        session,
        StorageResolver(session),
        ActionAllowList(Action.READ_ENVIRONMENT, Action.READ_TABLE),
    )
    assert reader.list_uploads(principal, "dev").items == ()


def test_a_list_does_not_read_a_manifest(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"x": b"x"}))
    statements: list[str] = []

    @event.listens_for(session.get_bind(), "before_cursor_execute")
    def _seen(_conn: object, _cur: object, statement: str, *_: object) -> None:
        statements.append(statement)

    service.list_uploads(principal, "dev")
    assert [s for s in statements if "manifest_json" in s] == []
    assert any("artifact_uploads" in s for s in statements)
