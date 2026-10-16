"""The artifact store on Postgres and SeaweedFS over real HTTP.

Multipart uploads, the commit guard's race, the bound token, retention, deletion and the sweep.
Nothing here installs a package; 'train, then predict' with the real harness is in
`test_python_assets_integration.py`.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
import pyarrow as pa
import pytest
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import lakana
from lakana.errors import NotFound
from lakana.errors import PermissionDenied as SdkPermissionDenied
from lakana_core.artifacts.sweep import sweep_artifacts
from lakana_core.db import create_engine, db_now
from lakana_core.models import (
    ArtifactBlob,
    ArtifactUpload,
    ArtifactVersion,
    AuditEvent,
    AuditEventType,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    JobVersion,
    Namespace,
    Repository,
    RepositoryKind,
    Table,
)
from lakana_core.projects.code_sweep import sweep_code_archives
from lakana_core.storage.resolver import StorageResolver
from lakana_server.app import create_app
from tests.integration_helpers import backend_pid, unique_name, wait_for_lock_waiter
from tests.python_asset_helpers import (
    TOKEN,
    Services,
    bound_attempt,
    reclaim,
    running_services,
    serving,
)

pytestmark = pytest.mark.integration

_PART = 8 * 1024 * 1024
_JOIN_SECONDS = 60
_IDS = pa.schema([pa.field("id", pa.int64())])
_GONE = "This upload has expired or does not exist; start the upload again."


class _WatchBlobBodies:
    """ASGI middleware: say when the route first reads the body of a blob upload.

    The route authenticates the caller, takes an upload slot and looks the upload up before it
    reads a byte of the body, so the event means the server holds the request.

    Parameters
    ----------
    app
        The application.
    started
        Set on the first `receive()` of a request to a blob route.
    """

    def __init__(self, app: ASGIApp, started: threading.Event) -> None:
        self.app = app
        self.started = started

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass a request on; for a blob PUT, set the event when its body is first read.

        Parameters
        ----------
        scope
            The ASGI scope.
        receive
            The ASGI receive channel.
        send
            The ASGI send channel.
        """
        if scope["type"] != "http" or scope["method"] != "PUT" or "/blobs/" not in scope["path"]:
            await self.app(scope, receive, send)
            return

        async def watched() -> Message:
            """Report that the body is being read, then receive.

            Returns
            -------
            Message
                The next ASGI message.
            """
            self.started.set()
            return await receive()

        await self.app(scope, watched, send)


@dataclass(frozen=True)
class _Stack:
    """The services, a running server, and an admin client.

    Parameters
    ----------
    services
        Postgres and SeaweedFS.
    url
        The server's base URL.
    admin
        An SDK client with the bootstrap token.
    db
        An engine on the metadata database, for the test's own reads and writes.
    body_started
        Set by `_WatchBlobBodies` when the server first reads the body of a blob request; the
        test clears it before the request it waits for.
    """

    services: Services
    url: str
    admin: lakana.Client
    db: Engine
    body_started: threading.Event

    def client(self, token: str = TOKEN) -> lakana.Client:
        """Open another SDK client on the server.

        Parameters
        ----------
        token
            The bearer token.

        Returns
        -------
        lakana.Client
            The client; the caller closes it.
        """
        return lakana.Client(self.url, token, timeout=60.0)

    def environment(self) -> str:
        """Create an environment with `bronze.events` (ids 1 and 2) and the namespace `models`.

        Returns
        -------
        str
            Its name.
        """
        name = unique_name()
        self.services.environment(self.admin, name)
        self.admin.tables.create(name, "bronze.events", schema=_IDS)
        self.admin.tables.append(name, "bronze.events", pa.table({"id": pa.array([1, 2])}))
        self.admin.namespaces.create(name, "models")
        return name

    def base(self, environment: str, name: str = "churn") -> str:
        """Build the URL of an artifact in the `models` namespace.

        Parameters
        ----------
        environment
            The environment.
        name
            The artifact's name.

        Returns
        -------
        str
            The URL without a trailing part.
        """
        return f"{self.url}/api/v1/environments/{environment}/namespaces/models/artifacts/{name}"

    def viewer(self, environment: str, *, read: bool = True) -> str:
        """Create a service account and return its token.

        Parameters
        ----------
        environment
            The environment.
        read
            Grant the `VIEWER` role (which reads artifacts); otherwise only `READ_ENVIRONMENT`.

        Returns
        -------
        str
            The account's personal access token.
        """
        name = f"acct-{uuid.uuid4().hex[:8]}"
        self.admin.principals.create(name, "SERVICE_ACCOUNT")
        if read:
            self.admin.grants.grant(
                principal=name, role="VIEWER", kind="ENVIRONMENT", environment=environment
            )
        else:
            self.admin.grants.grant(
                principal=name,
                action="READ_ENVIRONMENT",
                kind="ENVIRONMENT",
                environment=environment,
            )
        return self.admin.tokens.issue(name, name).token

    def artifact_id(self, session: Session, environment: str, name: str) -> uuid.UUID:
        """Find an artifact's id in one environment.

        Parameters
        ----------
        session
            A session on the metadata database.
        environment
            The environment.
        name
            The artifact's name in the `models` namespace.

        Returns
        -------
        uuid.UUID
            The id of the artifact's `tables` row.
        """
        return session.scalars(
            select(Table.id)
            .join(Namespace, Table.namespace_id == Namespace.id)
            .join(Environment, Namespace.environment_id == Environment.id)
            .where(Environment.name == environment, Table.name == name)
        ).one()

    def stored(self, environment: str, artifact_id: uuid.UUID, part: str = "") -> list[str]:
        """List the objects under an artifact's prefix in object storage.

        SeaweedFS keeps the empty directories of a deleted artifact, and a listing then reports
        the prefix itself as an object of size 0; that entry is no stored file and is left out.

        Parameters
        ----------
        environment
            The environment.
        artifact_id
            The artifact.
        part
            A sub-prefix such as ``/blobs``.

        Returns
        -------
        list of str
            The paths of the stored files.
        """
        prefix = f"artifacts/{artifact_id.hex}{part}"
        with Session(self.db) as session:
            storage = StorageResolver(session).for_environment(environment).storage
            return [o.path for o in storage.list_objects(prefix) if o.path.rstrip("/") != prefix]


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    """Start Postgres, SeaweedFS and a real HTTP server.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    _Stack
        The stack.
    """
    with running_services(tmp_path_factory) as services:
        db = create_engine(services.settings.database)
        app = create_app(services.settings)
        body_started = threading.Event()
        app.add_middleware(_WatchBlobBodies, started=body_started)
        try:
            with serving(app) as url, lakana.Client(url, TOKEN, timeout=60.0) as admin:
                yield _Stack(
                    services=services,
                    url=url,
                    admin=admin,
                    db=db,
                    body_started=body_started,
                )
        finally:
            db.dispose()


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    """Write a small tree.

    Parameters
    ----------
    root
        The directory to create.
    files
        Relative path to contents.

    Returns
    -------
    Path
        `root`.
    """
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def _document(files: dict[str, bytes]) -> dict[str, object]:
    """Build the manifest a hand-made client sends for some files.

    Parameters
    ----------
    files
        Path to contents.

    Returns
    -------
    dict
        The manifest document: format 1, a directory, one entry per file.
    """
    return {
        "format": 1,
        "is_directory": True,
        "files": [
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in files.items()
        ],
    }


def _auth(token: str) -> dict[str, str]:
    """Build the header of a bearer token.

    Parameters
    ----------
    token
        The token.

    Returns
    -------
    dict
        The request headers.
    """
    return {"Authorization": f"Bearer {token}"}


def test_a_file_larger_than_one_part_round_trips_through_object_storage(
    stack: _Stack, tmp_path: Path
) -> None:
    """12 MiB cross one multipart part; nothing is held whole."""
    env = stack.environment()
    out = _tree(tmp_path / "out", {"notes.txt": b"notes"})
    with (out / "weights.bin").open("wb") as handle:
        for index in range(12):
            handle.write(bytes([index]) * (1024 * 1024))
    first = stack.admin.artifacts.upload(env, "models.big", out)
    assert (first.version, first.file_count, first.size_bytes) == (1, 2, 12 * 1024 * 1024 + 5)
    got = stack.admin.artifacts.download(env, "models.big", tmp_path / "copy")
    with (
        (Path(got.path) / "weights.bin").open("rb") as copy,
        (out / "weights.bin").open("rb") as source,
    ):
        while block := source.read(1024 * 1024):
            assert copy.read(1024 * 1024) == block
        assert copy.read(1) == b""
    (out / "notes.txt").write_bytes(b"other notes")
    second = stack.admin.artifacts.upload(env, "models.big", out)
    assert (second.version, second.files_sent, second.files_reused) == (2, 1, 1)
    with Session(stack.db) as session:
        artifact_id = stack.artifact_id(session, env, "big")
        rows = session.scalar(
            select(func.count())
            .select_from(ArtifactBlob)
            .where(ArtifactBlob.artifact_id == artifact_id)
        )
        assert rows == 3
    assert len(stack.stored(env, artifact_id, "/blobs")) == 3


def test_an_aborted_upload_leaves_nothing_in_object_storage(stack: _Stack) -> None:
    """A blob that is too long or has the wrong bytes is refused and stores nothing."""
    env = stack.environment()
    body = b"model"
    digest = hashlib.sha256(body).hexdigest()
    base = stack.base(env, "aborted")
    document = {"format": 1, "is_directory": False, "files": _document({"m": body})["files"]}
    with httpx.Client(headers=_auth(TOKEN), timeout=60.0) as http:
        upload_id = http.post(f"{base}/uploads", json=document).json()["upload_id"]
        wrong = http.put(f"{base}/uploads/{upload_id}/blobs/{digest}", content=b"wrong")
        assert wrong.status_code == 422
        longer = http.put(f"{base}/uploads/{upload_id}/blobs/{digest}", content=b"x" * (_PART + 1))
        assert longer.status_code == 422
        shorter = http.put(f"{base}/uploads/{upload_id}/blobs/{digest}", content=b"mod")
        assert shorter.status_code == 422
    with Session(stack.db) as session:
        artifact_id = stack.artifact_id(session, env, "aborted")
        assert stack.stored(env, artifact_id) == []
        # The sweep covers every environment of the module's database, so its report says
        # nothing about this artifact alone: look at this artifact's own objects instead.
        sweep_artifacts(
            session, StorageResolver(session), now=db_now(session), upload_ttl_seconds=3600
        )
        assert stack.stored(env, artifact_id) == []
        assert (
            session.scalar(
                select(func.count())
                .select_from(ArtifactUpload)
                .where(ArtifactUpload.artifact_id == artifact_id)
            )
            == 1
        )


def test_a_stale_attempt_cannot_commit_over_rest(stack: _Stack) -> None:
    """A reclaim that lands while the commit runs wins; no version exists."""
    env = stack.environment()
    attempt = bound_attempt(
        stack.db, stack.admin, env, asset="models.churn", inputs={}, output="artifact"
    )
    body = b"model"
    digest = hashlib.sha256(body).hexdigest()
    base = stack.base(env)
    document = {"format": 1, "is_directory": False, "files": _document({"m": body})["files"]}
    outcome: dict[str, httpx.Response] = {}
    with httpx.Client(headers=_auth(attempt.token), timeout=60.0) as http:
        upload_id = http.post(f"{base}/uploads", json=document).json()["upload_id"]
        assert http.put(f"{base}/uploads/{upload_id}/blobs/{digest}", content=body).is_success
        with stack.db.connect() as holder:
            holder.execute(
                text("UPDATE task_runs SET claim_count = claim_count + 1 WHERE id = :id"),
                {"id": attempt.task_run_id},
            )
            thread = threading.Thread(
                target=lambda: outcome.update(
                    commit=http.post(f"{base}/uploads/{upload_id}/commit", json={})
                )
            )
            thread.start()
            wait_for_lock_waiter(stack.db, backend_pid(holder))
            holder.commit()
            thread.join(_JOIN_SECONDS)
        assert not thread.is_alive()
        assert outcome["commit"].status_code == 403
        assert "superseded" in outcome["commit"].json()["detail"]
        after = http.post(f"{base}/uploads", json=document)
        assert after.status_code == 401
    with Session(stack.db) as session:
        committed = session.scalars(
            select(ArtifactVersion).where(ArtifactVersion.task_run_id == attempt.task_run_id)
        ).all()
        assert committed == []
    assert stack.admin.artifacts.get(env, "models.churn").current_version is None
    assert stack.admin.artifacts.versions(env, "models.churn") == []


def _slow_put(
    stack: _Stack,
    base: str,
    token: str,
    upload_id: str,
    body: bytes,
    go: threading.Event,
    outcome: dict[str, httpx.Response],
) -> threading.Thread:
    """Start a blob request whose body stops after one byte until `go` is set.

    The server has authenticated the request (and holds an upload slot) from the moment the
    first byte is on its way, so the request cannot end as a 401 whatever happens meanwhile.

    Parameters
    ----------
    stack
        The stack.
    base
        The artifact's URL.
    token
        The bearer token of the attempt.
    upload_id
        The upload.
    body
        The file's contents.
    go
        Set by the test to let the rest of the body through.
    outcome
        Receives the response under ``put``.

    Returns
    -------
    threading.Thread
        The running request, once the server holds its slot.

    Raises
    ------
    AssertionError
        If the server does not take the request within a minute.
    """
    digest = hashlib.sha256(body).hexdigest()
    stack.body_started.clear()

    def chunks() -> Iterator[bytes]:
        """Yield one byte, wait for `go`, then yield the rest.

        Yields
        ------
        bytes
            A piece of the body.

        Raises
        ------
        AssertionError
            If `go` is not set in time.
        """
        yield body[:1]
        if not go.wait(_JOIN_SECONDS):
            raise AssertionError("the test never let the body go on")
        yield body[1:]

    def send() -> None:
        """Send the request and keep its response."""
        with httpx.Client(headers=_auth(token), timeout=_JOIN_SECONDS) as http:
            outcome["put"] = http.put(
                f"{base}/uploads/{upload_id}/blobs/{digest}", content=chunks()
            )

    thread = threading.Thread(target=send)
    thread.start()
    if not stack.body_started.wait(_JOIN_SECONDS):
        raise AssertionError("the server never took the request")
    return thread


def test_a_later_claim_clears_the_open_uploads_of_a_dead_attempt(stack: _Stack) -> None:
    """Claim 1 is cut off mid-file, claim 2 stores version 1."""
    env = stack.environment()
    first = bound_attempt(
        stack.db, stack.admin, env, asset="models.churn", inputs={}, output="artifact"
    )
    body = b"model"
    digest = hashlib.sha256(body).hexdigest()
    base = stack.base(env)
    document = {"format": 1, "is_directory": False, "files": _document({"m": body})["files"]}
    with httpx.Client(headers=_auth(first.token), timeout=60.0) as http:
        stale = http.post(f"{base}/uploads", json=document).json()["upload_id"]
    go = threading.Event()
    outcome: dict[str, httpx.Response] = {}
    thread = _slow_put(stack, base, first.token, stale, body, go, outcome)
    second = reclaim(stack.db, first)
    with httpx.Client(headers=_auth(second.token), timeout=60.0) as http:
        fresh = http.post(f"{base}/uploads", json=document)
        assert fresh.status_code == 201
        fresh_id = fresh.json()["upload_id"]
        with Session(stack.db) as session:
            open_ids = {str(u.id) for u in session.scalars(select(ArtifactUpload)).all()}
        assert stale not in open_ids and fresh_id in open_ids
        go.set()
        thread.join(_JOIN_SECONDS)
        assert not thread.is_alive()
        assert outcome["put"].status_code == 409
        assert outcome["put"].json()["detail"] == _GONE
        assert http.put(f"{base}/uploads/{fresh_id}/blobs/{digest}", content=body).is_success
        committed = http.post(f"{base}/uploads/{fresh_id}/commit", json={})
        assert committed.status_code == 201
        assert committed.json()["version"] == 1
        # the commit is idempotent for its own upload, and the dead attempt's upload stays gone
        assert http.post(f"{base}/uploads/{fresh_id}/commit", json={}).json()["version"] == 1
    with Session(stack.db) as session:
        rows = session.scalars(
            select(ArtifactVersion).where(ArtifactVersion.task_run_id == first.task_run_id)
        ).all()
        assert [(row.version, row.claim) for row in rows] == [(1, 2)]


def test_a_dead_attempts_open_upload_does_not_count_against_the_cap(stack: _Stack) -> None:
    """15 uploads of others and one of a killed attempt fill the cap of 16."""
    env = stack.environment()
    first = bound_attempt(
        stack.db, stack.admin, env, asset="models.capped", inputs={}, output="artifact"
    )
    base = stack.base(env, "capped")
    with httpx.Client(headers=_auth(first.token), timeout=60.0) as http:
        assert http.post(f"{base}/uploads", json=_document({"m": b"killed"})).status_code == 201
    with httpx.Client(headers=_auth(TOKEN), timeout=60.0) as http:
        for number in range(15):
            made = http.post(f"{base}/uploads", json=_document({"m": f"person {number}".encode()}))
            assert made.status_code == 201
        full = http.post(f"{base}/uploads", json=_document({"m": b"one too many"}))
        assert full.status_code == 409
        assert "16 uploads in progress" in full.json()["detail"]
    second = reclaim(stack.db, first)
    with httpx.Client(headers=_auth(second.token), timeout=60.0) as http:
        assert http.post(f"{base}/uploads", json=_document({"m": b"retried"})).status_code == 201
        # a retry within the claim replaces its own upload instead of taking another slot
        assert http.post(f"{base}/uploads", json=_document({"m": b"again"})).status_code == 201
    with Session(stack.db) as session:
        artifact_id = stack.artifact_id(session, env, "capped")
        open_uploads = session.scalar(
            select(func.count())
            .select_from(ArtifactUpload)
            .where(
                ArtifactUpload.artifact_id == artifact_id,
                ArtifactUpload.committed_version.is_(None),
            )
        )
    assert open_uploads == 16


def _assert_refusals_match(
    stack: _Stack, env: str, token: str, existing: str, stored_sha: str
) -> None:
    """Assert `token` gets the same answer on every route for `existing` as for a missing name.

    Parameters
    ----------
    stack
        The stack.
    env
        The environment; `existing` is an artifact in it, `ghost` is not.
    token
        The caller's bearer token, which may not read or write `existing`.
    existing
        The name of an artifact that exists (a bare name in the `models` namespace).
    stored_sha
        The manifest hash of its version 1, which no answer may contain.
    """
    digest = hashlib.sha256(b"{}").hexdigest()
    other = str(uuid.uuid4())
    document = _document({"model.json": b"{}"})
    routes: list[tuple[str, str, bytes | None, dict[str, object] | None]] = [
        ("GET", "", None, None),
        ("GET", "/versions", None, None),
        ("GET", "/versions/1", None, None),
        ("GET", "/versions/current", None, None),
        ("GET", f"/versions/1/blobs/{digest}", None, None),
        ("DELETE", "/versions/1", None, None),
        ("DELETE", "", None, None),
        ("POST", "/uploads", None, document),
        ("PUT", f"/uploads/{other}/blobs/{digest}", b"{}", None),
        ("POST", f"/uploads/{other}/commit", None, {}),
        ("DELETE", f"/uploads/{other}", None, None),
    ]
    with httpx.Client(headers=_auth(token), timeout=60.0) as http:
        for method, suffix, content, body in routes:
            answers = []
            for name in (existing, "ghost"):
                response = http.request(
                    method, f"{stack.base(env, name)}{suffix}", content=content, json=body
                )
                answers.append(
                    (
                        response.status_code,
                        response.headers.get("content-type"),
                        response.text.replace(name, "<name>"),
                    )
                )
            assert answers[0] == answers[1], (method, suffix)
            assert answers[0][0] == 403, (method, suffix, answers[0])
            assert stored_sha not in answers[0][2], (method, suffix)


def test_a_task_token_reads_only_declared_artifacts_and_writes_only_its_own(
    stack: _Stack, tmp_path: Path
) -> None:
    """A refusal over real HTTP with a real bound token."""
    env = stack.environment()
    model = _tree(tmp_path / "m", {"model.json": b"{}"})
    stack.admin.artifacts.upload(env, "models.declared", model)
    stack.admin.artifacts.upload(env, "models.other", model)
    attempt = bound_attempt(
        stack.db,
        stack.admin,
        env,
        asset="gold.scores",
        inputs={"model": "models.declared"},
        input_kinds={"model": "artifact"},
    )
    with stack.client(attempt.token) as task:
        got = task.artifacts.download(env, "models.declared", tmp_path / "copy", version=1)
        assert (Path(got.path) / "model.json").read_bytes() == b"{}"
        assert [a.name for a in task.artifacts.list(env)] == ["declared"]
        texts = []
        for name in ("models.other", "models.ghost"):
            for call in (task.artifacts.get, task.artifacts.versions, task.artifacts.version):
                with pytest.raises(SdkPermissionDenied) as caught:
                    call(env, name)
                texts.append(str(caught.value).replace(name, "<name>"))
        assert len(set(texts)) == 1
        with pytest.raises(SdkPermissionDenied, match="A task's token may not DROP_TABLE\\."):
            task.artifacts.delete_version(env, "models.declared", 1)
        with pytest.raises(SdkPermissionDenied, match="A task's token may not DROP_TABLE\\."):
            task.artifacts.drop(env, "models.declared")
        again = task.artifacts.download(env, "models.declared", tmp_path / "again", version=1)
        assert (Path(again.path) / "model.json").read_bytes() == b"{}"
        with pytest.raises(SdkPermissionDenied, match="may write only its own asset"):
            task.artifacts.upload(env, "models.declared", model)
        with pytest.raises(
            SdkPermissionDenied, match="produces a table; it cannot add an artifact version"
        ):
            task.artifacts.upload(env, "gold.scores", model)
    # the same on every route, for the bound token on an existing artifact it did not declare
    # against one that does not exist: status, content type and the complete body
    other_stored = stack.admin.artifacts.version(env, "models.other", 1)
    _assert_refusals_match(stack, env, attempt.token, "other", str(other_stored.manifest_sha256))
    with Session(stack.db) as session:
        downloads = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.ARTIFACT_DOWNLOADED,
                AuditEvent.securable_environment == env,
            )
        ).all()
        assert downloads == []


def test_a_task_token_does_not_see_which_task_produced_a_version(
    stack: _Stack, tmp_path: Path
) -> None:
    """The consumer sees what it reads, not the run identifiers of its producer."""
    env = stack.environment()
    producer = bound_attempt(
        stack.db, stack.admin, env, asset="models.made", inputs={}, output="artifact"
    )
    with stack.client(producer.token) as task:
        stored = task.artifacts.upload(env, "models.made", _tree(tmp_path / "m", {"m": b"{}"}))
        assert stored.task_run_id is not None and stored.run_id is not None
    consumer = bound_attempt(
        stack.db,
        stack.admin,
        env,
        asset="gold.use",
        inputs={"model": "models.made"},
        input_kinds={"model": "artifact"},
    )
    with stack.client(consumer.token) as task:
        seen = task.artifacts.version(env, "models.made", 1)
        listed = task.artifacts.versions(env, "models.made")
    for version in (seen, *listed):
        assert (version.task_run_id, version.run_id, version.project_version_id) == (
            None,
            None,
            None,
        )
        assert version.commit_sha == "a" * 40
    full = stack.admin.artifacts.version(env, "models.made", 1)
    assert full.task_run_id == str(producer.task_run_id)
    assert full.run_id == str(producer.run_id)


def test_a_principal_without_read_cannot_list_versions_or_download(
    stack: _Stack, tmp_path: Path
) -> None:
    """A viewer without read access is told nothing about the artifact."""
    env = stack.environment()
    stack.admin.artifacts.upload(env, "models.churn", _tree(tmp_path / "m", {"model.json": b"{}"}))
    with stack.client(stack.viewer(env, read=False)) as blind:
        assert blind.artifacts.list(env) == []
        with pytest.raises(SdkPermissionDenied):
            blind.artifacts.versions(env, "models.churn")
        with pytest.raises(SdkPermissionDenied):
            blind.artifacts.download(env, "models.churn", tmp_path / "copy")
    assert not (tmp_path / "copy").exists()


def test_a_refusal_is_the_same_for_an_existing_and_a_missing_artifact_on_every_route(
    stack: _Stack, tmp_path: Path
) -> None:
    """Status and complete body, existing against missing."""
    env = stack.environment()
    stored = stack.admin.artifacts.upload(
        env, "models.secret", _tree(tmp_path / "m", {"model.json": b"{}"})
    )
    _assert_refusals_match(
        stack, env, stack.viewer(env, read=False), "secret", str(stored.manifest_sha256)
    )
    # a reader that may see it gets a real answer, so the test above is not a blanket refusal
    reader = stack.viewer(env)
    with httpx.Client(headers=_auth(reader), timeout=60.0) as http:
        assert http.get(stack.base(env, "secret")).status_code == 200
        assert http.get(stack.base(env, "ghost")).status_code == 404


def test_keep_versions_deletes_bytes_and_keeps_the_record(stack: _Stack, tmp_path: Path) -> None:
    """`keep_versions` deletes the bytes and keeps the record, on object storage."""
    env = stack.environment()
    attempt = bound_attempt(
        stack.db,
        stack.admin,
        env,
        asset="models.churn",
        inputs={},
        output="artifact",
        keep_versions=2,
    )
    with stack.client(attempt.token) as task:
        for number in (1, 2, 3):
            tree = _tree(
                tmp_path / f"v{number}",
                {"model.json": str(number).encode(), "shared.bin": b"same"},
            )
            stored = task.artifacts.upload(env, "models.churn", tree)
            assert stored.version == number
        assert stored.deleted_versions == [1]
    versions = stack.admin.artifacts.versions(env, "models.churn")
    assert [(v.version, v.deleted_at is None) for v in versions] == [
        (3, True),
        (2, True),
        (1, False),
    ]
    assert versions[2].manifest_sha256
    with pytest.raises(NotFound, match="was deleted; its bytes are gone"):
        stack.admin.artifacts.download(env, "models.churn", tmp_path / "old", version=1)
    for number in (2, 3):
        got = stack.admin.artifacts.download(
            env, "models.churn", tmp_path / f"copy{number}", version=number
        )
        assert (Path(got.path) / "model.json").read_bytes() == str(number).encode()
        assert (Path(got.path) / "shared.bin").read_bytes() == b"same"
    with Session(stack.db) as session:
        artifact_id = stack.artifact_id(session, env, "churn")
    names = {path.rsplit("/", 1)[1] for path in stack.stored(env, artifact_id, "/blobs")}
    assert names == {hashlib.sha256(data).hexdigest() for data in (b"2", b"3", b"same")}


def test_deleting_dropping_and_the_sweep_on_object_storage(stack: _Stack, tmp_path: Path) -> None:
    """Deleting a version keeps a shared file; the sweep finds nothing; drop removes the prefix."""
    env = stack.environment()
    one = _tree(tmp_path / "a", {"m": b"one", "w": b"shared"})
    two = _tree(tmp_path / "b", {"m": b"two", "w": b"shared"})
    stack.admin.artifacts.upload(env, "models.churn", one)
    stack.admin.artifacts.upload(env, "models.churn", two)
    stack.admin.artifacts.delete_version(env, "models.churn", 1)
    with Session(stack.db) as session:
        artifact_id = stack.artifact_id(session, env, "churn")
        before = sorted(stack.stored(env, artifact_id))
        assert before
        sweep_artifacts(
            session, StorageResolver(session), now=db_now(session), upload_ttl_seconds=3600
        )
        # Scoped to this artifact: the sweep also visits what other tests left in the database.
        assert sorted(stack.stored(env, artifact_id)) == before
    got = stack.admin.artifacts.download(env, "models.churn", tmp_path / "copy")
    assert (Path(got.path) / "w").read_bytes() == b"shared"
    stack.admin.artifacts.drop(env, "models.churn")
    assert stack.stored(env, artifact_id) == []
    assert stack.admin.artifacts.list(env) == []


_RULES = [
    ("a parent directory", "../x", "'..'"),
    ("an absolute path", "/etc/passwd", "/etc/passwd"),
    ("a backslash", "a\\b", "backslash"),
    ("a dot part", "a/./b", "'.'"),
    ("an empty part", "a//b", "empty"),
    ("a control character", "a\x07b", "control"),
    ("a part of 256 bytes", "x" * 256, "255 bytes"),
    ("a path of 1025 bytes", "/".join(["abcdefgh"] * 114), "1024"),
    ("33 parts", "/".join(["a"] * 33), "32"),
]


@pytest.mark.parametrize(("what", "path", "fragment"), _RULES, ids=[r[0] for r in _RULES])
def test_a_hand_made_manifest_that_leaves_its_directory_is_refused(
    stack: _Stack, what: str, path: str, fragment: str
) -> None:
    """The server's own rules, for a client that did not run the SDK's scan."""
    env = stack.environment()
    name = f"rules{uuid.uuid4().hex[:8]}"
    with httpx.Client(headers=_auth(TOKEN), timeout=60.0) as http:
        refused = http.post(f"{stack.base(env, name)}/uploads", json=_document({path: b"x"}))
    assert refused.status_code == 422, refused.text
    assert fragment in refused.json()["detail"], refused.json()
    with Session(stack.db) as session:
        assert session.scalars(select(Table).where(Table.name == name)).all() == []


def test_manifests_that_collide_or_break_a_limit_are_refused_naming_the_setting(
    stack: _Stack,
) -> None:
    """A manifest over a limit is refused before a byte is sent."""
    env = stack.environment()
    base = stack.base(env, "limits")
    one = hashlib.sha256(b"x").hexdigest()
    too_big = {"path": "m", "size": 2**31 + 1, "sha256": one}
    digests = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(10001)]
    documents = {
        "LAKANA_ARTIFACTS__MAX_FILE_BYTES": [too_big],
        "LAKANA_ARTIFACTS__MAX_BYTES": [
            {"path": f"f{i}", "size": 2**31, "sha256": digests[i]} for i in range(6)
        ],
        "LAKANA_ARTIFACTS__MAX_FILES": [
            {"path": f"f{i}", "size": 1, "sha256": digests[i]} for i in range(10001)
        ],
        "at least one file": [],
        "Model.bin and model.bin": [
            {"path": "Model.bin", "size": 1, "sha256": one},
            {"path": "model.bin", "size": 1, "sha256": one},
        ],
        "Directories A and a differ only by case": [
            {"path": "A/x", "size": 1, "sha256": one},
            {"path": "a/y", "size": 1, "sha256": one},
        ],
    }
    with httpx.Client(headers=_auth(TOKEN), timeout=60.0) as http:
        for fragment, files in documents.items():
            refused = http.post(
                f"{base}/uploads", json={"format": 1, "is_directory": True, "files": files}
            )
            assert refused.status_code == 422, (fragment, refused.text)
            assert fragment in refused.json()["detail"], (fragment, refused.json())
        # Unicode folding: the composed and the decomposed 'é' are one name
        folded = http.post(
            f"{base}/uploads",
            json={
                "format": 1,
                "is_directory": True,
                "files": [
                    {"path": "café", "size": 1, "sha256": one},
                    {"path": "café", "size": 1, "sha256": one},
                ],
            },
        )
        assert folded.status_code == 422, folded.text


def test_a_manifest_request_over_16_mib_is_refused(stack: _Stack) -> None:
    """The body of `POST .../uploads` is read with a cap."""
    env = stack.environment()
    base = stack.base(env, "huge")
    pieces = 17

    def body() -> Iterator[bytes]:
        """Yield 17 MiB of spaces, one mebibyte at a time.

        Yields
        ------
        bytes
            A mebibyte of spaces.
        """
        for _ in range(pieces):
            yield b" " * (1024 * 1024)

    with httpx.Client(headers=_auth(TOKEN), timeout=60.0) as http:
        refused = http.post(f"{base}/uploads", content=body())
    assert refused.status_code == 413, refused.text
    with Session(stack.db) as session:
        assert session.scalars(select(Table).where(Table.name == "huge")).all() == []


def test_a_blob_that_the_manifest_does_not_name_or_another_principal_sends_is_refused(
    stack: _Stack,
) -> None:
    """The blob route trusts neither the hash in the path nor the upload id."""
    env = stack.environment()
    base = stack.base(env, "strict")
    body = b"model"
    digest = hashlib.sha256(body).hexdigest()
    stranger = stack.viewer(env)
    with httpx.Client(headers=_auth(TOKEN), timeout=60.0) as http:
        upload_id = http.post(f"{base}/uploads", json=_document({"m": body})).json()["upload_id"]
        unnamed = hashlib.sha256(b"other").hexdigest()
        refused = http.put(f"{base}/uploads/{upload_id}/blobs/{unnamed}", content=b"other")
        assert refused.status_code == 422
        assert "names no file with that SHA-256" in refused.json()["detail"]
        malformed = http.put(f"{base}/uploads/{upload_id}/blobs/{digest.upper()}", content=body)
        assert malformed.status_code == 422
        missing = http.put(f"{base}/uploads/{uuid.uuid4()}/blobs/{digest}", content=body)
        assert (missing.status_code, missing.json()["detail"]) == (409, _GONE)
    # someone who may read but not write cannot use the upload
    with httpx.Client(headers=_auth(stranger), timeout=60.0) as http:
        denied = http.put(f"{base}/uploads/{upload_id}/blobs/{digest}", content=body)
        assert denied.status_code == 403
    assert stack.stored(env, _artifact(stack, env, "strict"), "/blobs") == []


def _artifact(stack: _Stack, environment: str, name: str) -> uuid.UUID:
    """Find an artifact's id with a short session.

    Parameters
    ----------
    stack
        The stack.
    environment
        The environment.
    name
        The artifact's name in the `models` namespace.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    with Session(stack.db) as session:
        return stack.artifact_id(session, environment, name)


def test_a_download_is_audited_once_per_principal_and_version(
    stack: _Stack, tmp_path: Path
) -> None:
    """One event with the counts, not one per file."""
    env = stack.environment()
    files = {"a": b"one", "b": b"two"}
    stack.admin.artifacts.upload(env, "models.counted", _tree(tmp_path / "v1", files))

    def events() -> list[AuditEvent]:
        """Read the environment's download events, oldest first.

        Returns
        -------
        list of AuditEvent
            The events.
        """
        with Session(stack.db) as session:
            return list(
                session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.event_type == AuditEventType.ARTIFACT_DOWNLOADED,
                        AuditEvent.securable_environment == env,
                    )
                    .order_by(AuditEvent.occurred_at)
                )
            )

    stack.admin.artifacts.download(env, "models.counted", tmp_path / "c1")
    (first,) = events()
    assert (first.detail["file_count"], first.detail["size_bytes"], first.detail["version"]) == (
        2,
        6,
        1,
    )
    assert "sha256" not in first.detail and "a" not in first.detail
    stack.admin.artifacts.download(env, "models.counted", tmp_path / "c2")
    assert len(events()) == 1
    with stack.client(stack.viewer(env)) as other:
        other.artifacts.download(env, "models.counted", tmp_path / "c3")
    assert len(events()) == 2
    stack.admin.artifacts.upload(env, "models.counted", _tree(tmp_path / "v2", {"a": b"new"}))
    stack.admin.artifacts.download(env, "models.counted", tmp_path / "c4")
    assert [e.detail["version"] for e in events()] == [1, 1, 2]
    with Session(stack.db) as session:
        session.execute(
            update(AuditEvent)
            .where(AuditEvent.id == first.id)
            .values(occurred_at=first.occurred_at - timedelta(hours=2))
        )
        session.commit()
    stack.admin.artifacts.download(env, "models.counted", tmp_path / "c5", version=1)
    assert [e.detail["version"] for e in events()].count(1) == 3


def test_a_protected_environment_takes_the_task_and_refuses_a_person(
    stack: _Stack, tmp_path: Path
) -> None:
    """A task adds a version in a protected environment; a person needs a reason."""
    env = stack.environment()
    attempt = bound_attempt(
        stack.db, stack.admin, env, asset="models.guarded", inputs={}, output="artifact"
    )
    stack.admin.environments.set_protection(env, True)
    tree = _tree(tmp_path / "m", {"m": b"{}"})
    with stack.client(attempt.token) as task:
        assert task.artifacts.upload(env, "models.guarded", tree).version == 1
    with pytest.raises(SdkPermissionDenied, match="is protected"):
        stack.admin.artifacts.upload(env, "models.guarded", tree)
    with pytest.raises(SdkPermissionDenied, match="is protected"):
        stack.admin.artifacts.delete_version(env, "models.guarded", 1)
    with pytest.raises(SdkPermissionDenied, match="is protected"):
        stack.admin.artifacts.drop(env, "models.guarded")
    reason = "restoring the model by hand, ticket 4711"
    assert (
        stack.admin.artifacts.upload(env, "models.guarded", tree, break_glass_reason=reason).version
        == 2
    )
    assert re.fullmatch(
        r"[0-9a-f]{64}", stack.admin.artifacts.version(env, "models.guarded", 2).manifest_sha256
    )


def test_the_code_archive_sweep_removes_only_unnamed_old_archives_on_real_storage(
    stack: _Stack,
) -> None:
    """Only an old archive no project version names goes.

    A named one, a young one, a foreign file and anything a deploy could record stay.

    Real object-store modification times: "old" is a sweep time eight days ahead of the
    database's clock, so the test needs no sleep.
    """
    env = stack.environment()
    sha_named, sha_stray = "1" * 40, "2" * 40
    named = f"_artifacts/code/analytics/{sha_named}-{'c' * 64}.tar.gz"
    stray = f"_artifacts/code/analytics/{sha_stray}-{'d' * 64}.tar.gz"
    foreign = "_artifacts/code/analytics/README.txt"
    with Session(stack.db) as session:
        resolved = StorageResolver(session).for_environment(env)
        for path in (named, stray, foreign):
            resolved.storage.write(path, b"archive")
        environment = session.scalars(select(Environment).where(Environment.name == env)).one()
        repository = Repository(
            id=uuid.uuid4(), name=unique_name(), kind=RepositoryKind.HTTPS, url="https://h/r.git"
        )
        session.add(repository)
        session.flush()
        session.add(
            JobVersion(
                id=uuid.uuid4(),
                repository_id=repository.id,
                environment_id=environment.id,
                commit_sha=sha_named,
                manifest_json={},
                code_uri=f"{environment.storage_root.rstrip('/')}/{named}",
                code_sha256="c" * 64,
            )
        )
        run = DeployRun(
            id=uuid.uuid4(),
            environment_id=environment.id,
            repository_id=repository.id,
            kind=DeployKind.DEPLOY,
            status=DeployStatus.RUNNING,
        )
        session.add(run)
        session.commit()
        now = db_now(session)
        later = now + timedelta(days=8)
        resolver = StorageResolver(session)

        def present() -> set[str]:
            return {o.path for o in resolved.storage.list_objects("_artifacts/code") if o.size}

        everything = {named, stray, foreign}
        sweep_code_archives(session, resolver, now=now)
        assert present() == everything  # real modification times are young
        sweep_code_archives(session, resolver, now=later)
        assert present() == everything  # a deploy is running: it could record the stray one
        run.status = DeployStatus.SUCCEEDED
        session.commit()
        report = sweep_code_archives(session, resolver, now=later)
        assert report.deleted >= 1
        assert present() == {named, foreign}
