"""Shared test fixtures for lakana-worker."""

import hashlib
import os
import signal
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from task_run_helpers import _CODE, _CODE_PATH, World, _entry

import lakana_worker.task_lease as task_lease_module
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DEFAULT_MAINTENANCE_TIMEOUT, DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.models import (
    Base,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
)
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import executor as executor_module
from lakana_worker import pipeline as pipeline_module
from lakana_worker.query_process import REGISTRY, QueryChild
from lakana_worker.runners import TASKS


@pytest.fixture(autouse=True)
def _no_real_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `main()` from starting a memory watchdog inside the pytest process.

    A real one would exit the whole test run, with the watchdog's exit code, if the suite's
    resident memory ever passed the default limit. The real mechanism is tested in a child
    process (`test_memory.py`).

    Parameters
    ----------
    monkeypatch
        The pytest fixture.
    """
    monkeypatch.setattr("lakana_worker.__main__.start_watchdog", lambda *args: None)
    monkeypatch.setattr("lakana_worker.__main__.sweep_spill_directories", lambda: 0)
    monkeypatch.setattr("lakana_worker.__main__.sweep_deploy_checkouts", lambda **kwargs: 0)


@pytest.fixture
def without_o_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the restore as on macOS: no `O_PATH`, but a `chmod` relative to a directory descriptor.

    Linux's Python leaves `os.chmod` out of `os.supports_follow_symlinks`, yet its
    ``fchmodat(..., AT_SYMLINK_NOFOLLOW)`` works on a directory and refuses a symlink
    (a `ValueError`), so the macOS branch runs here for real, apart from what a link gets.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    """
    monkeypatch.delattr(os, "O_PATH", raising=False)
    monkeypatch.setattr("lakana_worker.processes._CHMOD_AT_WITHOUT_FOLLOWING", True)


@pytest.fixture
def session() -> Iterator[Session]:
    """Provide a session against a fresh in-memory SQLite database.

    The engine comes from `lakana_core.db.create_engine`, the same factory the
    server and catalog use, so foreign key enforcement here is the
    enforcement that ships rather than a fixture-only approximation of it.

    Yields
    ------
    Session
        A session whose schema has been created from the models.
    """
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def file_session(tmp_path: Path) -> Iterator[Session]:
    """Provide a session against a SQLite file, which a second session and thread can read.

    The in-memory database of `session` belongs to one thread's connection. A test of the
    cancel flag needs another session (the cancel request) and the poller's thread to see
    the same rows, as they do in a real database.

    Parameters
    ----------
    tmp_path
        Holds the database file.

    Yields
    ------
    Session
        A session whose schema has been created from the models.
    """
    database = tmp_path / "worker.db"
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{database}"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()
    for leftover in tmp_path.glob(f"{database.name}*"):
        leftover.unlink()  # /tmp is a RAM disk: nothing of the database stays after the test


@pytest.fixture(autouse=True)
def _no_drain_leaks() -> Iterator[None]:
    """Reset the executor's drain and stale-heartbeat callables before and after every test.

    `install_drain` and `install_heartbeat` set module globals, and `main()` (called by tests)
    installs real ones; a drain event that a test then sets would stop every later query after
    its grace, and a heartbeat that went stale would stop every later query at once.

    Yields
    ------
    None
        Once the test may run.
    """
    executor_module._DRAINING = None
    executor_module._WORKER_STALE = None
    yield
    executor_module._DRAINING = None
    executor_module._WORKER_STALE = None
    pipeline_module._MAINTENANCE_TIMEOUT = DEFAULT_MAINTENANCE_TIMEOUT
    pipeline_module._clock = time.monotonic


@pytest.fixture(autouse=True)
def _no_heartbeat_thread_leaks() -> Iterator[None]:
    """Fail a test that leaves a heartbeat thread running.

    Yields
    ------
    None
        Once the test may run.
    """
    yield
    left = [t.name for t in threading.enumerate() if t.name == "lakana-heartbeat"]
    assert left == [], "heartbeat threads left behind by the test"


@pytest.fixture(autouse=True)
def child_leak_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Fail a test that leaves a query child's process group or private directory behind.

    Every child leads its own session, so its process group is its process id. A group that
    still exists at teardown means the test (or the supervisor) did not reap its children.
    The children's private directories are made in a directory of the test's own, which must
    be empty at teardown: a failed clean-up is then caught here and never reaches `/tmp`.

    Parameters
    ----------
    monkeypatch
        Replaces the registration and the temp directory for the test.
    tmp_path_factory
        Makes the directory the private directories are created in.

    Yields
    ------
    None
        Once the test may run.
    """
    spill = tmp_path_factory.mktemp("spill")
    monkeypatch.setattr(tempfile, "tempdir", str(spill))
    started: list[int] = []
    registered: list[QueryChild] = []
    real_add, real_killpg, real_listdir = REGISTRY.add, os.killpg, os.listdir

    def add(child: QueryChild) -> None:
        """Remember the child's process id, then register it as usual.

        Parameters
        ----------
        child
            The child being registered.
        """
        assert child.pid is not None
        started.append(child.pid)
        registered.append(child)
        real_add(child)

    monkeypatch.setattr(REGISTRY, "add", add)
    yield
    left = []
    for pid in started:
        try:
            real_killpg(pid, signal.SIGKILL)  # a test may have replaced os.killpg
        except (ProcessLookupError, PermissionError):  # macOS: a group of exited processes
            continue
        left.append(pid)
    still_registered = REGISTRY.pids()
    # Record the failure first, then forget what the test left: its entries must not fail
    # the teardown of every test after it.
    for child in registered:
        REGISTRY.remove(child)
    assert left == [], "process groups left behind by the test"
    # A test may have replaced os.listdir, which Path.iterdir calls on Python 3.11.
    assert real_listdir(spill) == [], "private directories left behind by the test"
    assert still_registered == []
    leftover_tasks = TASKS.running()
    for task in leftover_tasks:
        TASKS.remove(task)
    assert leftover_tasks == [], "task processes left in the task registry by the test"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """Build the world; lease keepers poll every 20 ms.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Speeds up the keeper.

    Yields
    ------
    World
        The world, deploying `silver.clean` → `gold.daily`.
    """
    monkeypatch.setattr(task_lease_module, "_POLL_SECONDS", 0.02)
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    Base.metadata.create_all(engine)
    root = f"file://{tmp_path / 'storage'}"
    with Session(engine) as session:
        admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
        session.add(Principal(id=admin.id, type=admin.type, name=admin.name, is_active=True))
        session.commit()
        env = CatalogService(
            session, StorageResolver(session), AllowAllAuthorization()
        ).create_environment(
            admin, name="dev", storage=StorageConfig(scheme="file", root=root, options={})
        )
        StorageResolver(session).for_environment("dev").storage.write(_CODE_PATH, _CODE)
        repo = Repository(
            id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a.git"
        )
        session.add(repo)
        session.flush()
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json={},
            code_uri=f"{root}/{_CODE_PATH}",
            code_sha256=hashlib.sha256(_CODE).hexdigest(),
        )
        session.add(version)
        session.flush()
        session.add(
            EnvironmentDeployment(
                environment_id=env.id, repository_id=repo.id, current_job_version_id=version.id
            )
        )
        session.commit()
        built = World(engine, admin, root, version.id)
    built.set_manifest(
        _entry("silver.clean", "SELECT 1::BIGINT AS id"),
        _entry("gold.daily", "SELECT id FROM silver.clean"),
        edges=[["silver.clean", "gold.daily"]],
    )
    yield built
    engine.dispose()


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the webhook client reach a test server on the loopback interface.

    This is the only thing that ever sets `lakana_worker.egress._TEST_ALLOW_LOOPBACK`
    (`test_egress.py` greps the source tree to keep it that way). A test must ask for it.

    Parameters
    ----------
    monkeypatch
        The pytest fixture; undoes the flag after the test.
    """
    monkeypatch.setattr("lakana_worker.egress._TEST_ALLOW_LOOPBACK", True)
