"""The sweep of code archives no project version names."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from lakana_core.artifacts.sweep import ORPHAN_BLOB_GRACE
from lakana_core.models import (
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    JobVersion,
    Repository,
    RepositoryKind,
)
from lakana_core.projects import code_sweep
from lakana_core.projects.code_sweep import sweep_code_archives
from lakana_core.storage.base import StoredObject
from lakana_core.storage.resolver import ResolvedStorage, StorageResolver

_NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
_OLD = ORPHAN_BLOB_GRACE.total_seconds() + 3600
_SHA = "a" * 40
_DIGEST = "b" * 64


def _archive(repository: str = "analytics", sha: str = _SHA, digest: str = _DIGEST) -> str:
    """Return the environment-relative path of a code archive.

    Parameters
    ----------
    repository
        The repository's name.
    sha
        The commit.
    digest
        The archive's SHA-256.

    Returns
    -------
    str
        The path a deploy writes.
    """
    return f"_artifacts/code/{repository}/{sha}-{digest}.tar.gz"


class World:
    """Environments, a repository and a way to put archives into storage.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Directory under which the storage roots are made.
    """

    def __init__(self, session: Session, tmp_path: Path) -> None:
        self.session = session
        self.tmp_path = tmp_path
        self.repository = Repository(
            id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/r.git"
        )
        session.add(self.repository)
        session.flush()
        self.environments: dict[str, Environment] = {}

    def environment(self, name: str, root: str = "shared") -> Environment:
        """Create an environment whose storage root is a directory.

        Parameters
        ----------
        name
            The environment's name.
        root
            The directory's name; environments with the same name share a root.

        Returns
        -------
        Environment
            The environment.
        """
        directory = self.tmp_path / root
        directory.mkdir(exist_ok=True)
        env = Environment(id=uuid.uuid4(), name=name, storage_root=f"file://{directory}")
        self.session.add(env)
        self.session.flush()
        self.environments[name] = env
        return env

    def put(self, env: str, path: str, *, age: float | None = _OLD, data: bytes = b"tar") -> Path:
        """Write an object of a given age into an environment's storage.

        Parameters
        ----------
        env
            The environment's name.
        path
            Root-relative path.
        age
            Seconds before `_NOW` its mtime is set to; `None` leaves the real time.
        data
            The contents.

        Returns
        -------
        Path
            The file on disk.
        """
        resolved = StorageResolver(self.session).for_environment(env)
        resolved.storage.write(path, data)
        file = Path(resolved.config.root.removeprefix("file://")) / path
        if age is not None:
            moment = (_NOW - timedelta(seconds=age)).timestamp()
            os.utime(file, (moment, moment))
        return file

    def name(self, env: str, path: str, *, uri: str | None = None, sha: str | None = None) -> None:
        """Record a project version of an environment that names an archive.

        Parameters
        ----------
        env
            The environment's name.
        path
            The archive's root-relative path.
        uri
            The recorded ``code_uri``; the one a deploy builds when `None`.
        sha
            The recorded commit; a random one when `None`.
        """
        environment = self.environments[env]
        self.session.add(
            JobVersion(
                id=uuid.uuid4(),
                repository_id=self.repository.id,
                environment_id=environment.id,
                commit_sha=sha or uuid.uuid4().hex + "0" * 8,
                manifest_json={},
                code_uri=uri or f"{environment.storage_root.rstrip('/')}/{path}",
                code_sha256=_DIGEST,
            )
        )
        self.session.commit()

    def deploy(self, env: str, status: DeployStatus, kind: DeployKind = DeployKind.DEPLOY) -> None:
        """Record a deploy run.

        Parameters
        ----------
        env
            The environment's name.
        status
            The run's status.
        kind
            The run's kind.
        """
        self.session.add(
            DeployRun(
                id=uuid.uuid4(),
                environment_id=self.environments[env].id,
                repository_id=self.repository.id,
                kind=kind,
                status=status,
            )
        )
        self.session.commit()


@pytest.fixture
def world(session: Session, tmp_path: Path) -> World:
    """Provide a repository and the helpers to build a scene.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Pytest's temporary directory.

    Returns
    -------
    World
        The scene builder.
    """
    return World(session, tmp_path)


def _sweep(world: World, *, should_stop: Callable[[], bool] | None = None) -> int:
    """Run the sweep at `_NOW` and return how many archives it removed.

    Parameters
    ----------
    world
        The scene.
    should_stop
        Passed to `sweep_code_archives`.

    Returns
    -------
    int
        The number of archives removed.
    """
    world.session.commit()
    report = sweep_code_archives(
        world.session, StorageResolver(world.session), now=_NOW, should_stop=should_stop
    )
    return report.deleted


def test_an_old_archive_no_job_version_names_is_removed(world: World) -> None:
    world.environment("dev")
    file = world.put("dev", _archive())
    assert _sweep(world) == 1
    assert not file.exists()


def test_an_archive_a_job_version_names_stays(world: World) -> None:
    world.environment("dev")
    file = world.put("dev", _archive())
    world.name("dev", _archive())
    assert _sweep(world) == 0
    assert file.exists()


def test_a_young_unnamed_archive_stays(world: World) -> None:
    world.environment("dev")
    file = world.put("dev", _archive(), age=ORPHAN_BLOB_GRACE.total_seconds() - 60)
    assert _sweep(world) == 0
    assert file.exists()


def test_an_archive_named_only_by_an_environment_sharing_the_root_stays(world: World) -> None:
    world.environment("dev")
    world.environment("prod")
    file = world.put("dev", _archive())
    world.name("prod", _archive())
    assert _sweep(world) == 0
    assert file.exists()


def test_a_name_in_another_root_does_not_keep_an_archive(world: World) -> None:
    world.environment("dev", root="a")
    world.environment("prod", root="b")
    stray = world.put("dev", _archive())
    kept = world.put("prod", _archive())
    world.name("prod", _archive())
    assert _sweep(world) == 1
    assert not stray.exists()
    assert kept.exists()


def test_a_trailing_slash_on_the_root_does_not_hide_a_name(world: World) -> None:
    env = world.environment("dev")
    env.storage_root += "/"
    world.session.commit()
    file = world.put("dev", _archive())
    world.name("dev", _archive())
    assert _sweep(world) == 0
    assert file.exists()


@pytest.mark.parametrize(
    "path",
    [
        "_artifacts/code/analytics/notes.txt",
        f"_artifacts/code/analytics/{_SHA}-{_DIGEST}.tar",
        f"_artifacts/code/analytics/{_SHA}-{_DIGEST}.tar.gz.part",
        f"_artifacts/code/analytics/{'A' * 40}-{_DIGEST}.tar.gz",
        f"_artifacts/code/analytics/{'a' * 39}-{_DIGEST}.tar.gz",
        f"_artifacts/code/{_SHA}-{_DIGEST}.tar.gz",
        f"_artifacts/code/a/b/{_SHA}-{_DIGEST}.tar.gz",
    ],
)
def test_a_foreign_file_under_the_prefix_stays(
    world: World, path: str, caplog: pytest.LogCaptureFixture
) -> None:
    world.environment("dev")
    file = world.put("dev", path)
    with caplog.at_level(logging.DEBUG):
        assert _sweep(world) == 0
    assert file.exists()


def test_nothing_outside_the_prefix_is_touched(world: World) -> None:
    world.environment("dev")
    other = world.put("dev", f"_artifacts/other/{_SHA}-{_DIGEST}.tar.gz")
    table = world.put("dev", f"managed/t/{_SHA}-{_DIGEST}.tar.gz")
    assert _sweep(world) == 0
    assert other.exists() and table.exists()


@pytest.mark.parametrize("status", [DeployStatus.QUEUED, DeployStatus.RUNNING])
def test_a_queued_or_running_deploy_keeps_everything_it_could_record(
    world: World, status: DeployStatus
) -> None:
    world.environment("dev")
    world.environment("prod")
    file = world.put("dev", _archive())
    world.deploy("prod", status)
    assert _sweep(world) == 0
    assert file.exists()


def test_a_deploy_run_that_cannot_upload_or_has_ended_does_not_keep_anything(
    world: World,
) -> None:
    world.environment("dev")
    for status in (DeployStatus.SUCCEEDED, DeployStatus.FAILED):
        world.deploy("dev", status)
    world.deploy("dev", DeployStatus.RUNNING, DeployKind.DRY_RUN)
    world.deploy("dev", DeployStatus.QUEUED, DeployKind.ROLLBACK)
    file = world.put("dev", _archive())
    assert _sweep(world) == 1
    assert not file.exists()


def test_a_deploy_in_another_root_keeps_nothing_here(world: World) -> None:
    world.environment("dev", root="a")
    world.environment("prod", root="b")
    file = world.put("dev", _archive())
    world.deploy("prod", DeployStatus.RUNNING)
    assert _sweep(world) == 1
    assert not file.exists()


def test_a_deploy_that_starts_before_the_delete_keeps_the_archive(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deploy rewrites an old object and records it: only the run row protects it."""
    world.environment("dev")
    file = world.put("dev", _archive())
    real = code_sweep._active_deploy
    calls: list[int] = []

    def active(session: Session, environment_ids: list[uuid.UUID]) -> bool:
        calls.append(1)
        if len(calls) == 2:
            world.deploy("dev", DeployStatus.RUNNING)
        return real(session, environment_ids)

    monkeypatch.setattr(code_sweep, "_active_deploy", active)
    assert _sweep(world) == 0
    assert file.exists()


def test_a_job_version_recorded_before_the_delete_keeps_the_archive(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.environment("dev")
    file = world.put("dev", _archive())
    real = code_sweep._active_deploy
    calls: list[int] = []

    def active(session: Session, environment_ids: list[uuid.UUID]) -> bool:
        calls.append(1)
        found = real(session, environment_ids)
        if len(calls) == 2:
            world.name("dev", _archive())
        return found

    monkeypatch.setattr(code_sweep, "_active_deploy", active)
    assert _sweep(world) == 0
    assert file.exists()


def test_a_failing_delete_does_not_raise_and_the_others_go_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.environment("dev")
    first_path = _archive(sha="1" * 40)
    first = world.put("dev", first_path)
    second = world.put("dev", _archive(sha="2" * 40))
    real = StorageResolver.for_environment

    def resolve(self: StorageResolver, environment: str) -> ResolvedStorage:
        resolved = real(self, environment)
        original = resolved.storage.delete

        def delete(path: str) -> None:
            if path == first_path:
                raise OSError(5, "Input/output error")
            original(path)

        monkeypatch.setattr(resolved.storage, "delete", delete)
        return resolved

    monkeypatch.setattr(StorageResolver, "for_environment", resolve)
    assert _sweep(world) == 1
    assert first.exists()
    assert not second.exists()


def test_a_failing_listing_does_not_raise_and_the_other_root_is_swept(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.environment("dev", root="a")
    world.environment("prod", root="b")
    world.put("dev", _archive())
    file = world.put("prod", _archive())
    real = StorageResolver.for_environment

    def resolve(self: StorageResolver, environment: str) -> ResolvedStorage:
        resolved = real(self, environment)
        if environment == "dev":

            def broken(prefix: str) -> Iterator[StoredObject]:
                raise OSError("storage down")

            monkeypatch.setattr(resolved.storage, "list_objects", broken)
        return resolved

    monkeypatch.setattr(StorageResolver, "for_environment", resolve)
    world.session.commit()
    report = sweep_code_archives(world.session, StorageResolver(world.session), now=_NOW)
    assert report.failed_roots == 1
    assert report.deleted == 1
    assert not file.exists()


def test_the_time_budget_is_honoured(world: World) -> None:
    world.environment("dev")
    files = [world.put("dev", _archive(sha=f"{i:040x}")) for i in range(5)]
    clock = iter([0.0, 0.0, 0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5])
    world.session.commit()
    report = sweep_code_archives(
        world.session,
        StorageResolver(world.session),
        now=_NOW,
        max_seconds=2.0,
        monotonic=lambda: next(clock, 99.0),
    )
    assert report.stopped_early
    assert report.deleted < 5
    assert sum(f.exists() for f in files) == 5 - report.deleted


def test_a_stop_request_ends_the_sweep_before_any_delete(world: World) -> None:
    world.environment("dev")
    file = world.put("dev", _archive())
    assert _sweep(world, should_stop=lambda: True) == 0
    assert file.exists()


def test_an_object_without_a_modification_time_stays(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.environment("dev")
    file = world.put("dev", _archive())
    real = StorageResolver.for_environment

    def resolve(self: StorageResolver, environment: str) -> ResolvedStorage:
        resolved = real(self, environment)
        original = resolved.storage.list_objects

        def listing(prefix: str) -> Iterator[StoredObject]:
            for found in original(prefix):
                yield type(found)(path=found.path, size=found.size, modified_at=None)

        monkeypatch.setattr(resolved.storage, "list_objects", listing)
        return resolved

    monkeypatch.setattr(StorageResolver, "for_environment", resolve)
    assert _sweep(world) == 0
    assert file.exists()


def test_the_removal_is_logged_with_count_and_bytes(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.environment("dev")
    world.put("dev", _archive(), data=b"x" * 1234)
    with caplog.at_level(logging.INFO):
        _sweep(world)
    assert "1 unreferenced code archive" in caplog.text
    assert "1234 bytes" in caplog.text
    assert str(world.tmp_path) not in caplog.text


def test_a_second_deploy_of_identical_code_rewrites_the_object_and_refreshes_its_age(
    world: World,
) -> None:
    """`Storage.write` replaces the object, so a redeploy's archive is young again."""
    world.environment("dev")
    file = world.put("dev", _archive())
    assert file.stat().st_mtime < (_NOW - ORPHAN_BLOB_GRACE).timestamp()
    StorageResolver(world.session).for_environment("dev").storage.write(_archive(), b"tar")
    assert file.stat().st_mtime > (_NOW - ORPHAN_BLOB_GRACE).timestamp()
    assert _sweep(world) == 0
    assert file.exists()


def test_an_archive_named_by_commit_and_digest_stays_whatever_root_the_uri_spells(
    world: World,
) -> None:
    """Two spellings of one location must not make each group delete the other's archives."""
    world.environment("dev", root="a")
    world.environment("prod", root="b")
    file = world.put("dev", _archive())
    world.name("prod", _archive(), uri=f"file:///elsewhere//{_archive()}", sha=_SHA)
    assert _sweep(world) == 0
    assert file.exists()


def test_a_job_version_with_the_same_digest_but_another_commit_does_not_keep_it(
    world: World,
) -> None:
    world.environment("dev", root="a")
    world.environment("prod", root="b")
    file = world.put("dev", _archive())
    world.name("prod", _archive(), uri=f"file:///elsewhere/{_archive()}", sha="9" * 40)
    assert _sweep(world) == 1
    assert not file.exists()


def test_the_commit_and_digest_rule_is_applied_again_before_the_delete(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.environment("dev", root="a")
    world.environment("prod", root="b")
    file = world.put("dev", _archive())
    real = code_sweep._active_deploy
    calls: list[int] = []

    def active(session: Session, environment_ids: list[uuid.UUID]) -> bool:
        calls.append(1)
        found = real(session, environment_ids)
        if len(calls) == 2:
            world.name("prod", _archive(), uri=f"file:///elsewhere//{_archive()}", sha=_SHA)
        return found

    monkeypatch.setattr(code_sweep, "_active_deploy", active)
    assert _sweep(world) == 0
    assert file.exists()


def test_a_busy_root_is_logged_so_that_an_always_busy_root_is_visible(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.environment("dev")
    world.put("dev", _archive())
    world.deploy("dev", DeployStatus.RUNNING)
    with caplog.at_level(logging.INFO):
        _sweep(world)
    assert "1 busy" in caplog.text


def test_five_deletes_that_fail_in_a_row_give_the_root_up(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.environment("dev")
    files = [world.put("dev", _archive(sha=f"{i:040x}")) for i in range(7)]
    attempts: list[str] = []
    real = StorageResolver.for_environment

    def resolve(self: StorageResolver, environment: str) -> ResolvedStorage:
        resolved = real(self, environment)

        def delete(path: str) -> None:
            attempts.append(path)
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(resolved.storage, "delete", delete)
        return resolved

    monkeypatch.setattr(StorageResolver, "for_environment", resolve)
    world.session.commit()
    report = sweep_code_archives(world.session, StorageResolver(world.session), now=_NOW)
    assert len(attempts) == 5
    assert report.failed_roots == 1
    assert report.deleted == 0
    assert all(f.exists() for f in files)


def test_a_database_without_environments_has_nothing_to_sweep(world: World) -> None:
    report = sweep_code_archives(world.session, StorageResolver(world.session), now=_NOW)
    assert report == code_sweep.CodeSweepReport()


def test_a_root_whose_storage_cannot_be_resolved_is_counted_and_skipped(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world.environment("dev")

    def broken(self: StorageResolver, environment: str) -> ResolvedStorage:
        raise OSError("no key")

    monkeypatch.setattr(StorageResolver, "for_environment", broken)
    with caplog.at_level(logging.WARNING):
        report = sweep_code_archives(world.session, StorageResolver(world.session), now=_NOW)
    assert (report.failed_roots, report.deleted) == (1, 0)
    assert "OSError" in caplog.text
