"""The late deploy on a SQLite file, seen through a second connection.

The deploy's finishing points lock the run and stop when it is no longer the caller's. These
tests run the sweep from another connection to the same file, so that the worker's session
holds a stale picture of its run, as a worker that was frozen and woke up does.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_projects_deploys import (
    _FINISHING,
    _KIND_OF,
    _finish_call,
    _nothing_was_recorded,
    _queued,
    _repo,
    env,  # ruff: ignore[unused-import] -- fixture
)

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine, db_now
from lakana_core.errors import ConflictError
from lakana_core.models import Base, DeployRun, DeployStatus, Worker
from lakana_core.projects.deploys import DeployService
from lakana_core.runs.store import DEPLOY_LOST_MESSAGE, end_lost_runs


@pytest.fixture
def engine_url(tmp_path: Path) -> str:
    """Return the URL of a SQLite file that only this test uses.

    Parameters
    ----------
    tmp_path
        Holds the database file.

    Returns
    -------
    str
        The URL.
    """
    return f"sqlite:///{tmp_path / 'hold.db'}"


@pytest.fixture
def session(engine_url: str) -> Iterator[Session]:
    """Provide the worker's session on a SQLite file (overrides the in-memory one).

    Parameters
    ----------
    engine_url
        The file's URL.

    Yields
    ------
    Session
        A session whose schema has been created from the models.
    """
    engine = create_engine(DatabaseConfig(url=engine_url))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def other(session: Session) -> Iterator[Session]:
    """Provide a second connection to the same file: the scheduler, or an observer.

    Parameters
    ----------
    session
        The worker's session; its engine is shared, its connection is not.

    Yields
    ------
    Session
        A session on its own connection.
    """
    with Session(session.get_bind()) as s:
        yield s


def _dead_worker(other: Session) -> None:
    """Register worker `w1` with a heartbeat older than the lease.

    Parameters
    ----------
    other
        The scheduler's session; committed here.
    """
    beat = db_now(other) - timedelta(seconds=120)
    other.add(Worker(id="w1", host="h", version="1", started_at=beat, heartbeat_at=beat))
    other.commit()


@pytest.mark.parametrize("operation", [*_FINISHING, "fail"])
def test_a_deploy_that_finishes_after_the_sweep_ended_it_changes_nothing(
    session: Session,
    other: Session,
    env: None,  # ruff: ignore[redefined-while-unused] -- fixture
    principal: PrincipalRef,
    operation: str,
) -> None:
    """Each of the four finishing points, called by a worker whose picture is stale, writes nothing.

    The worker loaded its run as `running`; the scheduler (another connection) then ended it;
    the worker's call must change nothing the second connection can see.
    """
    _repo(session)
    run = _queued(session, principal, _KIND_OF.get(operation, _KIND_OF["record_deploy"]))
    session.commit()
    session.refresh(run)
    assert run.status is DeployStatus.RUNNING
    _dead_worker(other)
    assert end_lost_runs(other, db_now(other), 60) == 1
    service = DeployService(session, AllowAllAuthorization())
    with pytest.raises(ConflictError, match="is no longer this worker's running run"):
        if operation == "fail":
            service.fail(run, "boom", worker_id="w1")
        else:
            _finish_call(service, principal, run, operation, worker_id="w1")
    other.expire_all()
    seen = other.get(DeployRun, run.id)
    assert seen is not None
    assert (seen.status, seen.error_message) == (DeployStatus.FAILED, DEPLOY_LOST_MESSAGE)
    assert seen.job_version_id is None
    _nothing_was_recorded(other)


def test_a_deploy_that_finishes_before_the_sweep_is_not_ended(
    session: Session,
    other: Session,
    env: None,  # ruff: ignore[redefined-while-unused] -- fixture
    principal: PrincipalRef,
) -> None:
    """The other order: the deploy commits first; the sweep then finds a succeeded run."""
    _repo(session)
    run = _queued(session, principal)
    _dead_worker(other)
    service = DeployService(session, AllowAllAuthorization())
    _finish_call(service, principal, run, "record_deploy", worker_id="w1")
    assert end_lost_runs(other, db_now(other), 60) == 0
    other.expire_all()
    seen = other.get(DeployRun, run.id)
    assert seen is not None and seen.status is DeployStatus.SUCCEEDED
    assert other.query(DeployRun).count() == 1


def test_the_hold_takes_the_writer_lock_on_sqlite(
    session: Session,
    other: Session,
    env: None,  # ruff: ignore[redefined-while-unused] -- fixture
    principal: PrincipalRef,
) -> None:
    """SQLite has no row lock and `sqlite3` begins a transaction at the first write only.

    A bare `SELECT … FOR UPDATE` would leave a gap between the check and the finishing
    writes; the hold therefore starts the transaction with a write, and no other connection
    can end the run until this one commits or rolls back.
    """
    _repo(session)
    run = _queued(session, principal)
    session.commit()
    other.connection().exec_driver_sql("PRAGMA busy_timeout=0")
    service = DeployService(session, AllowAllAuthorization())
    service._hold(run, "w1")
    with pytest.raises(OperationalError, match="locked"):
        other.execute(update(DeployRun).values(status=DeployStatus.FAILED))
    other.rollback()
    session.rollback()
    other.execute(update(DeployRun).values(status=DeployStatus.FAILED))
    other.commit()
    assert other.execute(select(DeployRun.status)).scalar_one() is DeployStatus.FAILED


def test_a_refused_hold_gives_the_lock_back(
    session: Session,
    other: Session,
    env: None,  # ruff: ignore[redefined-while-unused] -- fixture
    principal: PrincipalRef,
) -> None:
    """A caller that is refused must not keep other connections waiting."""
    _repo(session)
    run = _queued(session, principal)
    other.connection().exec_driver_sql("PRAGMA busy_timeout=0")
    service = DeployService(session, AllowAllAuthorization())
    with pytest.raises(ConflictError):
        service._hold(run, uuid.uuid4().hex)
    other.execute(update(DeployRun).values(status=DeployStatus.FAILED))
    other.commit()
