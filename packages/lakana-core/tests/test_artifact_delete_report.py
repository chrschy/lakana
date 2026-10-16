"""A delete says what it freed and what it kept, and why."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    ActionAllowList,
    artifact_document,
    artifact_service,
    store_version,
)
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

import lakana_core.artifacts.service as service_module
from lakana_core.artifacts.service import ArtifactService, KeptFiles, KeptReason
from lakana_core.artifacts.store import holders_of, manifest_path
from lakana_core.auth import Action, PrincipalRef
from lakana_core.models import ArtifactVersion, Table
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver

_SHARED = b"shared weights"
_ONE = {"model.json": b"one", "weights.bin": _SHARED}
_TWO = {"model.json": b"two", "weights.bin": _SHARED}
_THREE = {"model.json": b"three", "weights.bin": _SHARED}


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


def test_a_delete_reports_what_it_freed_and_the_file_another_version_keeps(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert (deleted.freed_files, deleted.freed_bytes) == (1, len(b"one"))
    assert deleted.kept == (
        KeptFiles(KeptReason.OTHER_VERSION, files=1, bytes=len(_SHARED), version=2),
    )
    assert (deleted.version, deleted.deleted_by) == (1, "admin")


def test_a_delete_of_a_version_nothing_shares_keeps_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert (deleted.freed_files, deleted.freed_bytes, deleted.kept) == (
        2,
        len(b"one") + len(_SHARED),
        (),
    )


def test_the_newest_version_that_uses_a_file_is_the_one_named(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    for files in (_ONE, _TWO, _THREE):
        store_version(service, principal, ARTIFACT_REF, files)
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert deleted.kept == (KeptFiles(KeptReason.OTHER_VERSION, 1, len(_SHARED), 3),)


def test_files_of_several_other_versions_are_grouped_by_version_newest_first(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "b": b"bb", "c": b"ccc"})
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"bb"})
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert deleted.kept == (
        KeptFiles(KeptReason.OTHER_VERSION, 1, 2, 3),
        KeptFiles(KeptReason.OTHER_VERSION, 1, 1, 2),
    )
    assert (deleted.freed_files, deleted.freed_bytes) == (1, 3)


def test_an_open_upload_that_names_a_file_keeps_it(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"one", "x": b"x"}))
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert (deleted.freed_files, deleted.freed_bytes) == (1, len(_SHARED))
    assert deleted.kept == (KeptFiles(KeptReason.OPEN_UPLOAD, 1, len(b"one"), None),)


def test_a_file_that_a_version_and_an_upload_both_name_is_reported_for_the_version(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"w": _SHARED, "o": b"one"}))
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert deleted.kept == (
        KeptFiles(KeptReason.OTHER_VERSION, 1, len(_SHARED), 2),
        KeptFiles(KeptReason.OPEN_UPLOAD, 1, len(b"one"), None),
    )


def test_an_expired_upload_keeps_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    now = [datetime(2026, 10, 5, 12, 0, tzinfo=UTC)]
    service = artifact_service(session, tmp_path, clock=lambda: now[0])
    store_version(service, principal, ARTIFACT_REF, _ONE)
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"one"}))
    now[0] += timedelta(hours=2)
    assert service.delete_version(principal, ARTIFACT_REF, 1).kept == ()


def test_a_caller_who_may_delete_but_not_read_learns_no_version_number(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A raw `DROP_TABLE` grant carries no `READ_TABLE`: no number, and no split by version."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "b": b"bb", "c": b"ccc"})
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"bb"})
    dropper = ArtifactService(
        session,
        StorageResolver(session),
        ActionAllowList(Action.DROP_TABLE, Action.READ_ENVIRONMENT),
    )
    deleted = dropper.delete_version(principal, ARTIFACT_REF, 1)
    assert deleted.kept == (KeptFiles(KeptReason.OTHER_VERSION, 2, 3, None),)
    assert set(vars(deleted.kept[0])) == {"reason", "files", "bytes", "version"}


def test_files_left_after_the_release_budget_are_reported_for_the_sweep(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "b": b"bb", "c": b"ccc"})
    monkeypatch.setattr(service_module, "RELEASE_BUDGET_FILES", 2)
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert deleted.freed_files + sum(k.files for k in deleted.kept) == 3
    assert deleted.freed_files == 2
    (left,) = deleted.kept
    assert (left.reason, left.files, left.version) == (KeptReason.LEFT_TO_SWEEP, 1, None)
    assert deleted.freed_bytes + left.bytes == 6


def test_files_whose_removal_failed_are_reported_for_the_sweep(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)

    def broken(self: FsspecStorage, path: str) -> None:
        raise OSError("storage is down at /secret/path")

    monkeypatch.setattr(FsspecStorage, "delete", broken)
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert (deleted.freed_files, deleted.freed_bytes) == (0, 0)
    assert deleted.kept == (
        KeptFiles(KeptReason.LEFT_TO_SWEEP, 2, len(b"one") + len(_SHARED), None),
    )


def test_a_damaged_manifest_of_the_deleted_version_is_left_to_the_sweep(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    session.execute(update(ArtifactVersion).values(manifest_json={"files": "damaged"}))
    session.commit()
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert (deleted.freed_files, deleted.freed_bytes) == (0, 0)
    assert deleted.kept == (
        KeptFiles(KeptReason.LEFT_TO_SWEEP, 2, len(b"one") + len(_SHARED), None),
    )


def test_a_damaged_manifest_still_counts_each_file_once_whatever_the_number_of_paths(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Like every other group: distinct files, not paths. Entries it cannot read count one each."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    digest = _sha(b"one")
    entries = [
        {"path": "a", "sha256": digest, "size": 3},
        {"path": "b", "sha256": digest, "size": 3},  # the same file under a second path
        {"path": "c", "sha256": "not a hash", "size": 5},
        "not even an entry",
    ]
    session.execute(update(ArtifactVersion).values(manifest_json={"files": entries}))
    session.commit()
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert (deleted.freed_files, deleted.freed_bytes) == (0, 0)
    assert deleted.kept == (KeptFiles(KeptReason.LEFT_TO_SWEEP, 3, 3 + 5, None),)


def test_a_damaged_manifest_of_another_version_is_left_to_the_sweep(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.version == 2)
        .values(manifest_json={"files": "damaged"})
    )
    session.commit()
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert deleted.freed_files == 0
    assert deleted.kept == (
        KeptFiles(KeptReason.LEFT_TO_SWEEP, 2, len(b"one") + len(_SHARED), None),
    )


def test_manifests_the_catalog_does_not_know_leave_the_decision_to_the_sweep(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    StorageResolver(session).for_environment("dev").storage.write(
        manifest_path(artifact_id, 9), b'{"files":[{"sha256":"%s"}]}' % _sha(b"one").encode()
    )
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert deleted.freed_files == 0
    assert deleted.kept == (
        KeptFiles(KeptReason.LEFT_TO_SWEEP, 2, len(b"one") + len(_SHARED), None),
    )


def test_a_manifest_size_that_is_not_a_number_counts_as_zero_bytes(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    row = session.scalars(select(ArtifactVersion)).one()
    row.manifest_json = {
        "files": [{"path": "a", "size": "big", "sha256": _sha(b"a")}],
    }
    session.commit()
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert (deleted.freed_files, deleted.freed_bytes, deleted.kept) == (1, 0, ())


def test_a_negative_manifest_size_counts_as_zero_bytes_in_a_damaged_manifest(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A stored size below 0 is undeclared: the bytes of the answer can never be negative."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    row = session.scalars(select(ArtifactVersion)).one()
    row.manifest_json = {
        "files": [
            {"path": "a", "size": -5, "sha256": "not a hash"},
            {"path": "b", "size": 7, "sha256": "also not a hash"},
        ],
    }
    session.commit()
    deleted = service.delete_version(principal, ARTIFACT_REF, 1)
    assert deleted.kept == (KeptFiles(KeptReason.LEFT_TO_SWEEP, 2, 7, None),)


def test_what_a_delete_reports_is_decided_before_the_transaction_commits(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The kept files are worked out under the artifact's lock, not by a query after it."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _ONE)
    store_version(service, principal, ARTIFACT_REF, _TWO)
    committed: list[bool] = []
    manifest_reads_after_commit: list[str] = []

    @event.listens_for(session, "after_commit")
    def _after_commit(_: Session) -> None:
        committed.append(True)

    @event.listens_for(session.get_bind(), "before_cursor_execute")
    def _statement(_conn: object, _cur: object, statement: str, *_: object) -> None:
        if committed and ("artifact_versions" in statement or "artifact_uploads" in statement):
            manifest_reads_after_commit.append(statement)

    service.delete_version(principal, ARTIFACT_REF, 1)
    assert committed
    assert manifest_reads_after_commit == []


def test_a_file_nothing_names_is_absent_from_the_holders(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"u": b"u"}))
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    found = holders_of(
        session, artifact_id, {_sha(b"a"), _sha(b"u"), _sha(b"nobody")}, now=datetime.now(UTC)
    )
    assert found == {_sha(b"a"): 1, _sha(b"u"): None}
