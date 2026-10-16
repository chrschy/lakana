"""`client.artifacts` against the real application."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import lakana
from lakana.resources.artifacts import Artifacts
from lakana_core.models import ArtifactUpload


def test_upload_stores_a_version_and_a_second_one_reuses_files(
    lakana_client: lakana.Client, tmp_path: Path, storage_root: str
) -> None:
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    lakana_client.namespaces.create("dev", "models")
    out = tmp_path / "out"
    (out / "weights").mkdir(parents=True)
    (out / "model.json").write_bytes(b'{"factor": 2}\n')
    (out / "weights" / "w.bin").write_bytes(b"weights")
    first = lakana_client.artifacts.upload("dev", "models.churn", out)
    assert (first.version, first.file_count, first.is_directory, first.size_bytes) == (
        1,
        2,
        True,
        21,
    )
    assert [f.path for f in first.files] == ["model.json", "weights/w.bin"]
    (out / "model.json").write_bytes(b'{"factor": 3}\n')
    second = lakana_client.artifacts.upload("dev", "models.churn", out)
    assert (second.version, second.deleted_versions) == (2, [])
    single = lakana_client.artifacts.upload("dev", "models.single", out / "model.json")
    assert (single.version, single.is_directory, single.file_count) == (1, False, 1)


def test_download_round_trips_through_the_real_server(
    lakana_client: lakana.Client, tmp_path: Path, storage_root: str
) -> None:
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    lakana_client.namespaces.create("dev", "models")
    out = tmp_path / "out"
    (out / "weights").mkdir(parents=True)
    (out / "model.json").write_bytes(b'{"factor": 2}\n')
    (out / "weights" / "w.bin").write_bytes(b"weights")
    lakana_client.artifacts.upload("dev", "models.churn", out)
    (out / "model.json").write_bytes(b'{"factor": 3}\n')
    lakana_client.artifacts.upload("dev", "models.churn", out)

    got = lakana_client.artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert (got.version, got.file_count, got.size_bytes) == (2, 2, 21)
    assert (Path(got.path) / "model.json").read_bytes() == b'{"factor": 3}\n'
    assert (Path(got.path) / "weights" / "w.bin").read_bytes() == b"weights"
    old = lakana_client.artifacts.download("dev", "models.churn", tmp_path / "old", version=1)
    assert (Path(old.path) / "model.json").read_bytes() == b'{"factor": 2}\n'
    assert [a.name for a in lakana_client.artifacts.list("dev")] == ["churn"]
    assert lakana_client.artifacts.get("dev", "models.churn").versions == 2
    assert [v.version for v in lakana_client.artifacts.versions("dev", "models.churn")] == [2, 1]
    assert lakana_client.artifacts.version("dev", "models.churn").version == 2
    assert [t.kind for t in lakana_client.tables.list("dev", "models")] == ["ARTIFACT"]
    single = lakana_client.artifacts.upload("dev", "models.single", out / "model.json")
    one = lakana_client.artifacts.download(
        "dev", "models.single", tmp_path / "one", version=single.version
    )
    assert Path(one.path) == (tmp_path / "one" / "model.json").resolve()
    assert Path(one.path).read_bytes() == b'{"factor": 3}\n'


def test_a_file_that_is_damaged_in_storage_is_not_kept(
    lakana_client: lakana.Client, client: TestClient, tmp_path: Path, storage_root: str
) -> None:
    """The server cuts a body that is not the manifest's length; the client keeps nothing.

    The in-process client reports the server's error; a lenient one hands the SDK what a real
    connection would: the headers and the bytes sent before the cut.
    """
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    lakana_client.namespaces.create("dev", "models")
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.bin").write_bytes(b"0123456789")
    lakana_client.artifacts.upload("dev", "models.churn", out)
    (blob,) = Path(storage_root.removeprefix("file://")).rglob("blobs/*")
    blob.write_bytes(b"0123")
    lenient = TestClient(client.app, raise_server_exceptions=False)
    with (
        lakana.Client(
            url="http://testserver", token="lakana_pat_testkey_testsecret", http_client=lenient
        ) as sdk,
        pytest.raises(lakana.ArtifactCorrupt, match="does not match its manifest"),
    ):
        sdk.artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert not [p for p in tmp_path.iterdir() if "copy" in p.name]


def test_the_scan_follows_the_limits_the_operator_set(
    lakana_client: lakana.Client,
    client: TestClient,
    tmp_path: Path,
    storage_root: str,
) -> None:
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    lakana_client.namespaces.create("dev", "models")
    out = tmp_path / "out"
    out.mkdir()
    for name in ("a", "b", "c"):
        (out / name).write_bytes(name.encode())
    client.app.state.settings.artifacts.max_files = 2
    with (
        lakana.Client(
            url="http://testserver", token="lakana_pat_testkey_testsecret", http_client=client
        ) as strict,
        pytest.raises(lakana.ValidationError, match="more than the 2 files"),
    ):
        strict.artifacts.upload("dev", "models.churn", out)
    client.app.state.settings.artifacts.max_files = 10
    with lakana.Client(
        url="http://testserver", token="lakana_pat_testkey_testsecret", http_client=client
    ) as relaxed:
        assert relaxed.artifacts.upload("dev", "models.churn", out).file_count == 3


def test_delete_and_drop_through_the_real_server(
    lakana_client: lakana.Client, tmp_path: Path, storage_root: str
) -> None:
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    lakana_client.namespaces.create("dev", "models")
    target = tmp_path / "model.json"
    for body in (b"one", b"two", b"three"):
        target.write_bytes(body)
        lakana_client.artifacts.upload("dev", "models.churn", target)
    deleted = lakana_client.artifacts.delete_version("dev", "models.churn", 2)
    assert (deleted.version, deleted.deleted_by, deleted.files) == (2, "admin", [])
    assert deleted.deleted_at is not None
    assert (deleted.freed_files, deleted.freed_bytes, deleted.kept) == (1, 3, [])
    assert (deleted.kept_files, deleted.kept_bytes) == (0, 0)
    assert lakana_client.artifacts.get("dev", "models.churn").current_version == 3
    listed = lakana_client.artifacts.versions("dev", "models.churn")
    assert [(v.version, v.deleted_at is not None) for v in listed] == [
        (3, False),
        (2, True),
        (1, False),
    ]
    with pytest.raises(lakana.Conflict, match="already deleted"):
        lakana_client.artifacts.delete_version("dev", "models.churn", 2)
    with pytest.raises(lakana.NotFound):
        lakana_client.artifacts.delete_version("dev", "models.churn", 9)
    old = lakana_client.artifacts.download("dev", "models.churn", tmp_path / "old", version=1)
    assert Path(old.path).read_bytes() == b"one"
    lakana_client.artifacts.drop("dev", "models.churn")
    assert lakana_client.artifacts.list("dev") == []
    with pytest.raises(lakana.NotFound):
        lakana_client.artifacts.drop("dev", "models.churn")


def _open_uploads(client: TestClient) -> int:
    """Count the uploads that are still open.

    Parameters
    ----------
    client
        The test client whose app owns the database.

    Returns
    -------
    int
        The number of upload rows.
    """
    with Session(client.app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(ArtifactUpload)) or 0


def test_an_upload_that_fails_withdraws_itself_on_the_real_server(
    lakana_client: lakana.Client,
    client: TestClient,
    tmp_path: Path,
    storage_root: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    lakana_client.namespaces.create("dev", "models")
    target = tmp_path / "model.json"
    target.write_bytes(b"one")

    def broken(*_args: object, **_kwargs: object) -> None:
        raise lakana.ServerError("the network went away", status=503, error_type="x")

    monkeypatch.setattr(Artifacts, "_send", broken)
    with pytest.raises(lakana.ServerError, match="network went away"):
        lakana_client.artifacts.upload("dev", "models.churn", target)
    assert _open_uploads(client) == 0


def test_abort_upload_through_the_real_server(
    lakana_client: lakana.Client, client: TestClient, storage_root: str
) -> None:
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    lakana_client.namespaces.create("dev", "models")
    document = {
        "format": 1,
        "is_directory": False,
        "files": [{"path": "m", "size": 1, "sha256": "a" * 64}],
    }
    begun = client.post(
        "/api/v1/environments/dev/namespaces/models/artifacts/churn/uploads",
        headers={"Authorization": "Bearer lakana_pat_testkey_testsecret"},
        json=document,
    )
    assert begun.status_code == 201, begun.text
    assert _open_uploads(client) == 1
    upload_id = begun.json()["upload_id"]
    listed = lakana_client.artifacts.list_uploads("dev")
    assert [(u.upload_id, u.artifact) for u in listed.items] == [(upload_id, "models.churn")]
    assert listed.next_cursor is None
    assert [u.upload_id for u in lakana_client.artifacts.iter_uploads("dev", "models.churn")] == [
        upload_id
    ]
    assert lakana_client.artifacts.abort_upload("dev", "models.churn", upload_id) is None
    assert _open_uploads(client) == 0
    assert lakana_client.artifacts.list_uploads("dev").items == []
    with pytest.raises(lakana.Conflict, match="expired or does not exist"):
        lakana_client.artifacts.abort_upload("dev", "models.churn", upload_id)


def test_uploads_whose_commit_was_refused_do_not_block_the_artifact(
    lakana_client: lakana.Client, client: TestClient, tmp_path: Path, storage_root: str
) -> None:
    """A wrong source name is refused at the commit: sixteen of them must leave no upload open."""
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    lakana_client.namespaces.create("dev", "models")
    target = tmp_path / "model.json"
    target.write_bytes(b"m")
    for _ in range(17):
        with pytest.raises(lakana.NotFound):
            lakana_client.artifacts.upload(
                "dev", "models.churn", target, sources={"gold.typo": None}
            )
    assert _open_uploads(client) == 0
    assert lakana_client.artifacts.upload("dev", "models.churn", target).version == 1
