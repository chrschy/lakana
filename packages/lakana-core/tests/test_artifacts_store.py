"""Paths, the lock and the hash sets of the artifact store."""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    artifact_document,
    artifact_service,
    ensure_table,
    python_task_run,
    store_version,
)
from sqlalchemy import select, update
from sqlalchemy.engine.result import ScalarResult
from sqlalchemy.orm import Session

from lakana_core.artifacts import store
from lakana_core.artifacts.store import (
    UnreadableManifest,
    artifact_prefix,
    attempt_version,
    blob_path,
    blobs_prefix,
    checked_hashes,
    kind_of,
    lock_artifact,
    manifest_path,
    manifest_size,
    run_input_version,
    stored_hashes,
    unreferenced_hashes,
)
from lakana_core.auth import PrincipalRef, RunContext
from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import ArtifactUpload, ArtifactVersion, Table

_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
_A = "a" * 64


def test_paths_use_the_id_and_nothing_a_caller_chose() -> None:
    assert artifact_prefix(_ID) == "artifacts/12345678123456781234567812345678"
    assert blobs_prefix(_ID) == "artifacts/12345678123456781234567812345678/blobs"
    assert blob_path(_ID, _A) == f"artifacts/12345678123456781234567812345678/blobs/{_A}"
    assert manifest_path(_ID, 7) == "artifacts/12345678123456781234567812345678/manifests/7.json"


@pytest.mark.parametrize(
    "digest", ["", "A" * 64, "a" * 63, "a" * 65, "../" + "a" * 61, "a" * 63 + "/", "a" * 64 + "\n"]
)
def test_a_hash_that_is_not_a_hash_never_becomes_a_path(digest: str) -> None:
    with pytest.raises(ValidationError, match="64 hex digits"):
        blob_path(_ID, digest)


def test_the_lock_matches_only_an_artifact(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"m": b"m"}, is_directory=False)
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    _, table_id = ensure_table(session, "dev", "models", "plain")
    assert lock_artifact(session, artifact_id) is True
    assert lock_artifact(session, table_id) is False
    assert lock_artifact(session, uuid.uuid4()) is False
    session.rollback()
    assert session.scalars(select(Table.name).where(Table.id == artifact_id)).one() == "churn"


def test_unreferenced_hashes_ask_live_versions_and_open_uploads(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    now = [datetime(2026, 10, 4, 12, 0, tzinfo=UTC)]
    service = artifact_service(session, tmp_path, clock=lambda: now[0])
    one = store_version(service, principal, ARTIFACT_REF, {"a": b"a", "s": b"shared"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b", "s": b"shared"})
    open_upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"c": b"c"}))
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    digests = {f.path: f.sha256 for f in one.version.files}
    b_sha, c_sha = hashlib.sha256(b"b").hexdigest(), open_upload.missing[0]
    everything = {digests["a"], digests["s"], b_sha, c_sha, "f" * 64}
    assert unreferenced_hashes(session, artifact_id, everything, now=now[0]) == {"f" * 64}
    assert stored_hashes(session, artifact_id, [digests["a"], "f" * 64]) == {digests["a"]: 1}

    first = session.scalars(select(ArtifactVersion).where(ArtifactVersion.version == 1)).one()
    first.deleted_at = now[0]
    session.commit()
    later = unreferenced_hashes(session, artifact_id, everything, now=now[0] + timedelta(hours=2))
    assert later == {digests["a"], c_sha, "f" * 64}
    assert unreferenced_hashes(session, artifact_id, set(), now=now[0]) == set()
    assert everything >= {digests["a"]}  # the caller's set is never changed


def test_every_batch_of_both_queries_is_searched(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Stopping after the first batch with files left over would delete what is still named."""
    service = artifact_service(session, tmp_path)
    count = 2 * store._STREAM_ROWS + 1
    hashes: set[str] = set()
    for i in range(count):
        data = f"version {i}".encode()
        store_version(service, principal, ARTIFACT_REF, {"f": data})
        hashes.add(hashlib.sha256(data).hexdigest())
    for i in range(count):
        data = f"upload {i}".encode()
        upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"f": data}))
        hashes.add(hashlib.sha256(data).hexdigest())
        assert upload.missing
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    asked = hashes | {"f" * 64}
    assert unreferenced_hashes(session, artifact_id, asked, now=datetime.now(UTC)) == {"f" * 64}


def test_the_search_stops_as_soon_as_nothing_is_left_to_protect(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    for _ in range(3):
        store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    seen: list[object] = []
    real = store.checked_hashes
    monkeypatch.setattr(store, "checked_hashes", lambda d: seen.append(d) or real(d))
    digest = hashlib.sha256(b"a").hexdigest()
    assert unreferenced_hashes(session, artifact_id, {digest}, now=datetime.now(UTC)) == set()
    assert len(seen) == 1


def test_the_result_is_closed_when_a_manifest_in_the_middle_is_damaged(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    for data in (b"a", b"b", b"c"):
        store_version(service, principal, ARTIFACT_REF, {"f": data})
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.version == 2)
        .values(manifest_json={"files": "x"})
    )
    session.commit()
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    closed: list[int] = []
    real = ScalarResult.close
    monkeypatch.setattr(ScalarResult, "close", lambda self: (closed.append(1), real(self))[1])
    unrelated = {"9" * 64}
    with pytest.raises(UnreadableManifest):
        unreferenced_hashes(session, artifact_id, unrelated, now=datetime.now(UTC))
    assert closed


@pytest.mark.parametrize(
    "damaged",
    [
        {},
        [],
        None,
        {"files": "x"},
        {"files": []},
        {"files": [3]},
        {"files": [{"path": "p"}]},
        {"files": [{"sha256": 7}]},
        {"files": [{"sha256": _A}, {"sha256": "../../managed/keep"}]},
        {"files": [{"sha256": "A" * 64}]},
        {"files": [{"sha256": "a" * 64 + "\n"}]},
    ],
    ids=[
        "empty",
        "list",
        "none",
        "str",
        "no-files",
        "junk-entry",
        "no-hash",
        "int-hash",
        "path-hash",
        "upper",
        "newline",
    ],
)
def test_a_damaged_manifest_is_not_one_that_names_nothing(damaged: object) -> None:
    with pytest.raises(UnreadableManifest):
        checked_hashes(damaged)


def test_checked_hashes_reads_every_hash_of_a_sound_manifest() -> None:
    other = "b" * 64
    assert checked_hashes({"files": [{"sha256": _A}, {"sha256": other}, {"sha256": _A}]}) == {
        _A,
        other,
    }


def test_a_damaged_manifest_of_a_live_version_or_an_open_upload_stops_the_listing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Deleting on a set that silently leaves a manifest out would delete bytes it names."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    now = datetime.now(UTC)
    for damaged in ({}, {"files": "x"}, {"files": [3, {"sha256": 7}, {"path": "p"}]}, []):
        row = session.scalars(select(ArtifactVersion)).one()
        row.manifest_json = damaged  # ty: ignore[invalid-assignment]
        session.commit()
        with pytest.raises(UnreadableManifest):
            unreferenced_hashes(session, artifact_id, {_A}, now=now)
    row.deleted_at = now  # a deleted version's manifest no longer matters
    session.commit()
    assert unreferenced_hashes(session, artifact_id, {_A}, now=now) == {_A}
    session.add(
        ArtifactUpload(
            id=uuid.uuid4(),
            artifact_id=artifact_id,
            principal_id=principal.id,
            manifest_sha256="0" * 64,
            manifest_json={"files": "x"},
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    session.commit()
    with pytest.raises(UnreadableManifest):
        unreferenced_hashes(session, artifact_id, {_A}, now=now)
    # An expired upload protects nothing, so a damaged one of those does not matter either.
    assert unreferenced_hashes(session, artifact_id, {_A}, now=now + timedelta(hours=2)) == {_A}


def test_a_hash_of_another_type_never_becomes_a_path() -> None:
    for digest in (5, None, b"a" * 64, ["a" * 64]):
        with pytest.raises(ValidationError, match="64 hex digits"):
            blob_path(_ID, digest)  # ty: ignore[invalid-argument-type]


def test_manifest_size_reads_one_entry_and_the_first_one_wins() -> None:
    other = "b" * 64
    document = {
        "files": [
            "junk",
            {"sha256": 7},
            {"sha256": _A, "size": 3},
            {"sha256": _A, "size": "x"},
            {"sha256": other, "size": 0},
        ]
    }
    assert manifest_size(document, _A) == 3
    assert manifest_size(document, other) == 0
    assert manifest_size(document, "c" * 64) is None
    assert manifest_size(document, 5) is None  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "document",
    [None, [], {}, {"files": "x"}, {"files": {"a": 1}}],
    ids=["none", "list", "empty", "str", "dict"],
)
def test_manifest_size_refuses_a_document_without_a_list_of_files(document: object) -> None:
    with pytest.raises(ValueError, match="no list of files"):
        manifest_size(document, _A)


@pytest.mark.parametrize(
    "size",
    ["3", -1, True, 1.5, None, 10**20, 10**5000],
    ids=["str", "neg", "bool", "float", "none", "limit", "huge"],
)
def test_manifest_size_refuses_a_size_that_is_not_one(size: object) -> None:
    with pytest.raises(ValueError, match="no valid size"):
        manifest_size({"files": [{"sha256": _A, "size": size}]}, _A)
    assert manifest_size({"files": [{"sha256": _A, "size": 10**20 - 1}]}, _A) == 10**20 - 1


def test_a_consumer_reads_the_version_its_own_run_made_else_the_current_one(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The one committed by this run if the producer ran in it, otherwise the current."""
    service = artifact_service(session, tmp_path)
    ensure_table(session, "dev", "silver", "rows")
    task = python_task_run(
        session, principal.id, asset="models.churn", inputs={}, extra={"output": "artifact"}
    )
    actor = dataclasses.replace(principal, run_context=RunContext(task_run_id=task.id, claim=1))
    store_version(service, principal, ARTIFACT_REF, {"m": b"1"}, is_directory=False)
    store_version(service, actor, ARTIFACT_REF, {"m": b"2"}, is_directory=False)
    store_version(service, principal, ARTIFACT_REF, {"m": b"3"}, is_directory=False)

    assert run_input_version(session, "dev", "models.churn", task.run_id) == 2
    assert run_input_version(session, "dev", "models.churn", uuid.uuid4()) == 3
    assert attempt_version(session, "dev", "models.churn", task.id, 1) == 2
    assert attempt_version(session, "dev", "models.churn", task.id, 2) is None
    assert attempt_version(session, "dev", "models.churn", uuid.uuid4(), 1) is None

    service.delete_version(principal, ARTIFACT_REF, 2)
    assert run_input_version(session, "dev", "models.churn", task.run_id) == 3
    # A version this attempt committed counts although it was deleted since (pruned
    # by an overlapping run with `keep_versions`, or by hand); the consumer side above does not.
    assert attempt_version(session, "dev", "models.churn", task.id, 1) == 2
    assert attempt_version(session, "dev", "models.churn", task.id, 2) is None
    assert attempt_version(session, "dev", "models.churn", uuid.uuid4(), 1) is None
    for number in (1, 3):
        service.delete_version(principal, ARTIFACT_REF, number)
    assert run_input_version(session, "dev", "models.churn", task.run_id) is None


def test_an_attempts_and_a_runs_versions_are_those_of_the_named_artifact_only(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """I2: rows of another artifact with the same task run and claim do not leak across names.

    The service never lets one attempt commit to a second artifact, so the rows are made by
    giving a person's versions of `models.other` the attempt's task run and claim.
    """
    service = artifact_service(session, tmp_path)
    task = python_task_run(
        session, principal.id, asset="models.churn", inputs={}, extra={"output": "artifact"}
    )
    actor = dataclasses.replace(principal, run_context=RunContext(task_run_id=task.id, claim=1))
    other = TableRef.from_dotted("dev", "models.other")
    untouched = TableRef.from_dotted("dev", "models.untouched")
    store_version(service, actor, ARTIFACT_REF, {"m": b"1"}, is_directory=False)
    for body in (b"a", b"b", b"c"):
        store_version(service, principal, other, {"m": body}, is_directory=False)
    store_version(service, principal, untouched, {"m": b"x"}, is_directory=False)
    other_id = session.scalars(select(Table.id).where(Table.name == "other")).one()
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == other_id)
        .values(task_run_id=task.id, claim=1)
    )
    session.commit()
    assert attempt_version(session, "dev", "models.churn", task.id, 1) == 1
    assert attempt_version(session, "dev", "models.other", task.id, 1) == 3
    assert attempt_version(session, "dev", "models.untouched", task.id, 1) is None
    assert run_input_version(session, "dev", "models.churn", task.run_id) == 1
    assert run_input_version(session, "dev", "models.untouched", task.run_id) == 1


def test_kinds_and_names_that_are_not_artifacts(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"m": b"1"}, is_directory=False)
    ensure_table(session, "dev", "silver", "rows")
    assert kind_of(session, "dev", "models.churn") == "ARTIFACT"
    assert kind_of(session, "dev", "silver.rows") == "TABLE"
    for name in ("silver.nope", "nodots", "a..b", "nope.x"):
        assert kind_of(session, "dev", name) is None
    assert kind_of(session, "elsewhere", "models.churn") is None
    assert run_input_version(session, "dev", "silver.rows", uuid.uuid4()) is None
    assert attempt_version(session, "dev", "silver.rows", uuid.uuid4(), 1) is None
    assert run_input_version(session, "dev", "nodots", uuid.uuid4()) is None


def test_the_storage_root_of_artifacts_is_the_one_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reserved-location check of the storage layer depends on this name being the same."""
    from lakana_core.storage.resolver import ARTIFACTS_PREFIX

    artifact_id = uuid.uuid4()
    assert store.artifact_prefix(artifact_id) == f"{ARTIFACTS_PREFIX}/{artifact_id.hex}"
    monkeypatch.setattr(store, "ARTIFACTS_PREFIX", "elsewhere")
    assert store.artifact_prefix(artifact_id) == f"elsewhere/{artifact_id.hex}"
