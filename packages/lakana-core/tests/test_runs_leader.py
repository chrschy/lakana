"""Leader election for the scheduler role."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import Base, SchedulerLease
from lakana_core.runs.leader import (
    ADVISORY_LOCK_KEY,
    HeartbeatLock,
    LeaderElection,
    PostgresAdvisoryLock,
    lock_for,
)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[sqlalchemy.Engine]:
    """Build a file-backed SQLite engine with the schema, so two elections share one database.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Yields
    ------
    sqlalchemy.Engine
        The engine, disposed of after the test.
    """
    built = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lease.db"))
    Base.metadata.create_all(built)
    yield built
    built.dispose()


def test_one_holder_and_takeover_after_a_missed_heartbeat(engine: sqlalchemy.Engine) -> None:
    """SQLite: the conditional heartbeat update elects exactly one holder; stale → takeover."""
    a = LeaderElection(engine, "a")
    b = LeaderElection(engine, "b")
    assert a.try_lead(NOW) is True
    assert b.try_lead(NOW) is False
    assert a.try_lead(NOW + timedelta(seconds=5)) is True
    assert b.try_lead(NOW + timedelta(seconds=14)) is False
    assert b.try_lead(NOW + timedelta(seconds=16)) is True
    assert a.try_lead(NOW + timedelta(seconds=16)) is False
    with Session(engine) as session:
        lease = session.get(SchedulerLease, 1)
        assert lease is not None
        assert (lease.holder, lease.heartbeat_at) == ("b", NOW + timedelta(seconds=16))


def test_resign_frees_the_role_only_for_its_holder(engine: sqlalchemy.Engine) -> None:
    """A resigning non-holder changes nothing; the holder's resignation frees the role."""
    a = LeaderElection(engine, "a")
    b = LeaderElection(engine, "b")
    a.try_lead(NOW)
    b.resign()
    assert b.try_lead(NOW) is False
    a.resign()
    assert b.try_lead(NOW) is True


def test_lock_for_picks_the_advisory_lock_only_on_postgres(engine: sqlalchemy.Engine) -> None:
    """Postgres gets the advisory lock; everything else the heartbeat alone (no connection made)."""
    assert isinstance(lock_for(engine), HeartbeatLock)
    postgres = sqlalchemy.create_engine("postgresql+pg8000://u:p@localhost:1/db")
    assert isinstance(lock_for(postgres), PostgresAdvisoryLock)
    postgres.dispose()


class _FakeAdvisoryLocks:
    """`pg_try_advisory_lock`/`pg_advisory_unlock` as SQLite functions, one holder per key.

    A stand-in for the two Postgres functions so `PostgresAdvisoryLock`'s SQL
    runs in the unit tier; the integration tests prove the real semantics on Postgres.
    """

    def __init__(self) -> None:
        self.holders: dict[int, int] = {}

    def install(self, engine: sqlalchemy.Engine) -> None:
        """Register the functions on every new connection of `engine`.

        Parameters
        ----------
        engine
            A SQLite engine.
        """

        @event.listens_for(engine, "connect")
        def _register(dbapi_connection: sqlite3.Connection, record: object) -> None:
            ident = id(dbapi_connection)
            dbapi_connection.create_function(
                "pg_try_advisory_lock", 1, lambda key: self._try(key, ident)
            )
            dbapi_connection.create_function(
                "pg_advisory_unlock", 1, lambda key: self._unlock(key, ident)
            )

    def _try(self, key: int, ident: int) -> int:
        """Take `key` for connection `ident` if free (re-entrant).

        Parameters
        ----------
        key
            Lock key.
        ident
            Connection identity.

        Returns
        -------
        int
            1 when held by `ident` afterwards, else 0.
        """
        holder = self.holders.setdefault(key, ident)
        return int(holder == ident)

    def _unlock(self, key: int, ident: int) -> int:
        """Release `key` if `ident` holds it.

        Parameters
        ----------
        key
            Lock key.
        ident
            Connection identity.

        Returns
        -------
        int
            1 when released, else 0.
        """
        if self.holders.get(key) == ident:
            del self.holders[key]
            return 1
        return 0


@pytest.fixture
def advisory(engine: sqlalchemy.Engine) -> _FakeAdvisoryLocks:
    """Install the fake advisory-lock functions on `engine`.

    Parameters
    ----------
    engine
        The SQLite engine.

    Returns
    -------
    _FakeAdvisoryLocks
        The shared lock table.
    """
    locks = _FakeAdvisoryLocks()
    locks.install(engine)
    engine.dispose()  # pooled connections from create_all predate the functions
    return locks


def test_advisory_lock_has_one_holder_and_is_released(
    engine: sqlalchemy.Engine, advisory: _FakeAdvisoryLocks
) -> None:
    """One connection holds the key; release frees it for the next."""
    first = PostgresAdvisoryLock(engine)
    second = PostgresAdvisoryLock(engine)
    assert first.acquire() is True
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert ADVISORY_LOCK_KEY not in advisory.holders
    assert second.acquire() is True
    second.release()
    second.release()


def test_an_advisory_holder_writes_the_heartbeat_unconditionally(
    engine: sqlalchemy.Engine, advisory: _FakeAdvisoryLocks
) -> None:
    """With the lock held, a stale-looking row of another holder is overwritten."""
    LeaderElection(engine, "old").try_lead(NOW)
    election = LeaderElection(engine, "new", lock=PostgresAdvisoryLock(engine))
    assert election.try_lead(NOW) is True
    rival = LeaderElection(engine, "rival", lock=PostgresAdvisoryLock(engine))
    assert rival.try_lead(NOW + timedelta(hours=1)) is False
    election.resign()
    assert rival.try_lead(NOW) is True


def test_a_dead_lock_connection_loses_leadership_then_reacquires(
    engine: sqlalchemy.Engine, advisory: _FakeAdvisoryLocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed liveness check drops the connection and reports no lock; the next try reconnects."""
    lock = PostgresAdvisoryLock(engine)
    assert lock.acquire() is True
    connection = lock._connection
    assert connection is not None

    def broken(*args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    monkeypatch.setattr(connection, "execute", broken)
    assert lock.acquire() is False
    advisory.holders.clear()
    assert lock.acquire() is True
    lock.release()


def test_a_failing_lock_query_is_no_lock(engine: sqlalchemy.Engine) -> None:
    """Without the functions (or with a broken server) acquiring fails closed."""
    assert PostgresAdvisoryLock(engine).acquire() is False


def test_a_release_that_fails_still_drops_the_connection(
    engine: sqlalchemy.Engine, advisory: _FakeAdvisoryLocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unlock error is swallowed; the connection is invalidated either way."""
    lock = PostgresAdvisoryLock(engine)
    lock.acquire()
    connection = lock._connection
    assert connection is not None

    def broken(*args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT pg_advisory_unlock", {}, Exception("gone"))

    monkeypatch.setattr(connection, "execute", broken)
    lock.release()
    assert lock._connection is None


def test_a_resign_releases_the_advisory_lock(
    engine: sqlalchemy.Engine, advisory: _FakeAdvisoryLocks
) -> None:
    """Clean shutdown frees the lock key and the row for the next holder."""
    election = LeaderElection(engine, "a", lock=PostgresAdvisoryLock(engine))
    election.try_lead(NOW)
    assert ADVISORY_LOCK_KEY in advisory.holders
    election.resign()
    assert ADVISORY_LOCK_KEY not in advisory.holders


def test_a_concurrent_row_insert_is_tolerated(
    engine: sqlalchemy.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the race to create the lease row is not an error."""
    with Session(engine) as session:
        session.add(SchedulerLease(id=1))
        session.commit()

    original = Session.get
    misses = [1]

    def blind_get(self: Session, *args: object, **kwargs: object) -> object:
        """Miss once on the lease row, as if another process were inserting it, then behave.

        Parameters
        ----------
        self
            The session.
        *args
            Entity and identity.
        **kwargs
            Passed through.

        Returns
        -------
        object
            `None` the first time, else the row.
        """
        if misses[0]:
            misses[0] -= 1
            return None
        return original(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(Session, "get", blind_get)
    assert LeaderElection(engine, "a").try_lead(NOW) is True


def test_a_failing_resign_update_still_releases_the_lock(
    engine: sqlalchemy.Engine, advisory: _FakeAdvisoryLocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If clearing the heartbeat row raises, the advisory lock is released anyway."""
    election = LeaderElection(engine, "a", lock=PostgresAdvisoryLock(engine))
    election.try_lead(NOW)

    def broken(self: Session, *args: object, **kwargs: object) -> None:
        raise OperationalError("UPDATE", {}, Exception("gone"))

    monkeypatch.setattr(Session, "execute", broken)
    with pytest.raises(OperationalError):
        election.resign()
    assert ADVISORY_LOCK_KEY not in advisory.holders


def test_an_unreachable_server_is_no_lock(
    engine: sqlalchemy.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connect that raises fails closed instead of escaping `acquire`."""

    def refuse() -> None:
        raise OperationalError("connect", {}, Exception("connection refused"))

    monkeypatch.setattr(engine, "connect", refuse)
    assert PostgresAdvisoryLock(engine).acquire() is False


def test_the_advisory_lock_key_spells_the_project_name() -> None:
    """The key is the project's name as bytes, and fits Postgres's signed 64-bit lock key."""
    assert int.from_bytes(b"lakana", "big") == ADVISORY_LOCK_KEY
    assert ADVISORY_LOCK_KEY < 2**63


class _ResettingConnection:
    """A connection whose statements fail like pg8000's after the server cut the session."""

    def __init__(self) -> None:
        self.invalidated = False
        self.closed = False

    def execute(self, *args: object, **kwargs: object) -> object:
        """Fail like a socket the server reset.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        ConnectionResetError
            Always.
        """
        raise ConnectionResetError(104, "Connection reset by peer")

    def commit(self) -> None:
        """Nothing to commit."""

    def invalidate(self) -> None:
        """Record the discard."""
        self.invalidated = True

    def close(self) -> None:
        """Record the close."""
        self.closed = True


class _ConnectingEngine:
    """An engine stand-in whose `connect` returns or raises what the test says.

    Parameters
    ----------
    outcome
        The connection to return, or the exception to raise.
    """

    def __init__(self, outcome: object) -> None:
        self._outcome = outcome

    def connect(self) -> object:
        """Return the connection or raise the exception.

        Returns
        -------
        object
            The connection.

        Raises
        ------
        BaseException
            The configured exception.
        """
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def test_a_reset_of_the_held_connection_drops_it_and_reports_not_leader(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raw `OSError` after a server-side cut is treated like a database error."""
    lock = PostgresAdvisoryLock(cast("sqlalchemy.Engine", _ConnectingEngine(None)))
    held = _ResettingConnection()
    lock._connection = cast("sqlalchemy.Connection", held)
    with caplog.at_level(logging.INFO, logger="lakana_core.runs.leader"):
        assert lock.acquire() is False
    assert lock._connection is None and held.invalidated
    assert [r.exc_info for r in caplog.records] == [None]


def test_a_reset_while_connecting_reports_not_leader(caplog: pytest.LogCaptureFixture) -> None:
    """The same at the connect site: no exception escapes, no traceback is logged."""
    lock = PostgresAdvisoryLock(
        cast("sqlalchemy.Engine", _ConnectingEngine(ConnectionResetError(104, "reset")))
    )
    with caplog.at_level(logging.INFO, logger="lakana_core.runs.leader"):
        assert lock.acquire() is False
    assert lock._connection is None
    assert [r.exc_info for r in caplog.records] == [None]


def test_a_reset_while_taking_the_lock_discards_the_new_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same at the lock statement: the fresh connection is invalidated, not kept."""
    fresh = _ResettingConnection()
    lock = PostgresAdvisoryLock(cast("sqlalchemy.Engine", _ConnectingEngine(fresh)))
    with caplog.at_level(logging.INFO, logger="lakana_core.runs.leader"):
        assert lock.acquire() is False
    assert lock._connection is None and fresh.invalidated
    assert [r.exc_info for r in caplog.records] == [None]
