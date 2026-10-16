"""Leader election for the scheduler role.

Postgres: a session-level `pg_try_advisory_lock` on a fixed key, held on one
dedicated connection for as long as the holder lives — if its process dies,
the connection closes and the lock is free — plus a heartbeat row the API
reads. SQLite (local mode): the heartbeat row alone, taken by a conditional
update that succeeds only for the holder or once the holder's heartbeat is
stale.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import Connection, Engine, or_, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from lakana_core.db import rowcount
from lakana_core.models import SchedulerLease
from lakana_core.models.run import SCHEDULER_LEASE_ID

logger = logging.getLogger(__name__)

ADVISORY_LOCK_KEY = 0x6C616B616E61
"""``b"lakana"`` as a big-endian integer; the advisory lock's key (a Postgres ``bigint``)."""

STALE_AFTER = timedelta(seconds=10)
"""A heartbeat older than this frees the role (SQLite)."""


class SchedulerLock(Protocol):
    """The mutual-exclusion half of leader election."""

    exclusive: bool
    """Whether holding the lock alone proves leadership (then the heartbeat is unconditional)."""

    def acquire(self) -> bool:
        """Take or keep the lock.

        Returns
        -------
        bool
            Whether this process holds it now.
        """
        ...

    def release(self) -> None:
        """Give the lock up."""
        ...


class HeartbeatLock:
    """No lock: the conditional heartbeat update decides (SQLite)."""

    exclusive = False

    def acquire(self) -> bool:
        """Succeed always, leaving the heartbeat row as the arbiter.

        Returns
        -------
        bool
            `True`.
        """
        return True

    def release(self) -> None:
        """Nothing to release."""


class PostgresAdvisoryLock:
    """A session-level advisory lock on a dedicated connection.

    Parameters
    ----------
    engine
        The metadata database engine.
    key
        The lock key.
    """

    exclusive = True

    def __init__(self, engine: Engine, key: int = ADVISORY_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._connection: Connection | None = None

    def _drop(self, connection: Connection) -> None:
        """Discard the dedicated connection without returning it to the pool.

        Parameters
        ----------
        connection
            The held connection.
        """
        with contextlib.suppress(DBAPIError, OSError):
            connection.invalidate()
        self._connection = None

    @staticmethod
    def _log_lost(exc: BaseException) -> None:
        """Log that the role's connection failed, once per failure and without a traceback.

        Parameters
        ----------
        exc
            The error; only its type is logged.
        """
        logger.info(
            "The scheduler role's database connection failed (%s); trying again on the next tick.",
            type(exc).__name__,
        )

    def acquire(self) -> bool:
        """Keep the lock if held and the connection is alive, else try to take it.

        Returns
        -------
        bool
            Whether the lock is held after the call. A failed liveness check
            drops the connection and returns `False`; the next call tries again.

        Notes
        -----
        After the server cut a connection (it noticed the client was gone, or PostgreSQL
        restarted), the next write succeeds and the server answers with a reset. pg8000 then
        raises a raw `OSError` (`ConnectionResetError`) that SQLAlchemy does not wrap and
        `pool_pre_ping` does not catch. That is expected from this driver, so the three database
        calls here treat an `OSError` like a `DBAPIError`: one log line, no traceback.
        """
        held_connection = self._connection
        if held_connection is not None:
            try:
                held_connection.execute(text("SELECT 1"))
                held_connection.commit()
            except (DBAPIError, OSError) as exc:
                self._log_lost(exc)
                self._drop(held_connection)
                return False
            return True
        try:
            connection = self._engine.connect()
        except (DBAPIError, OSError) as exc:
            self._log_lost(exc)
            return False
        try:
            held = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
                ).scalar()
            )
            connection.commit()
        except (DBAPIError, OSError) as exc:
            self._log_lost(exc)
            connection.invalidate()
            return False
        if not held:
            connection.close()
            return False
        self._connection = connection
        return True

    def release(self) -> None:
        """Unlock and discard the connection; errors are swallowed."""
        connection = self._connection
        if connection is None:
            return
        with contextlib.suppress(DBAPIError):
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self._key})
            connection.commit()
        self._drop(connection)


def lock_for(engine: Engine) -> SchedulerLock:
    """Choose the lock for an engine's dialect.

    Parameters
    ----------
    engine
        The metadata database engine.

    Returns
    -------
    SchedulerLock
        `PostgresAdvisoryLock` for Postgres, `HeartbeatLock` otherwise.
    """
    if engine.dialect.name == "postgresql":
        return PostgresAdvisoryLock(engine)
    return HeartbeatLock()


class LeaderElection:
    """Decide, every tick, whether this worker holds the scheduler role.

    Parameters
    ----------
    engine
        The metadata database engine.
    holder
        This worker's id.
    lock
        The mutual-exclusion lock; `lock_for(engine)` when `None`.
    stale_after
        Heartbeat age that frees the role; `STALE_AFTER` when `None`.
    """

    def __init__(
        self,
        engine: Engine,
        holder: str,
        *,
        lock: SchedulerLock | None = None,
        stale_after: timedelta | None = None,
    ) -> None:
        self._engine = engine
        self.holder = holder
        self._lock = lock if lock is not None else lock_for(engine)
        self._stale_after = stale_after if stale_after is not None else STALE_AFTER

    @staticmethod
    def _ensure_row(session: Session) -> None:
        """Insert the lease row if a schema created without the migration lacks it.

        Parameters
        ----------
        session
            Database session.
        """
        if session.get(SchedulerLease, SCHEDULER_LEASE_ID) is not None:
            return
        session.add(SchedulerLease(id=SCHEDULER_LEASE_ID))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()

    def try_lead(self, now: datetime) -> bool:
        """Take, keep or fail to get the role, writing the heartbeat when held.

        Parameters
        ----------
        now
            The current time (injected).

        Returns
        -------
        bool
            Whether this worker holds the role for this tick.
        """
        if not self._lock.acquire():
            return False
        with Session(self._engine) as session:
            self._ensure_row(session)
            statement = update(SchedulerLease).where(SchedulerLease.id == SCHEDULER_LEASE_ID)
            if not self._lock.exclusive:
                statement = statement.where(
                    or_(
                        SchedulerLease.holder.is_(None),
                        SchedulerLease.holder == self.holder,
                        SchedulerLease.heartbeat_at < now - self._stale_after,
                    )
                )
            result = session.execute(
                statement.values(holder=self.holder, heartbeat_at=now).execution_options(
                    synchronize_session=False
                )
            )
            session.commit()
        return rowcount(result) == 1

    def resign(self) -> None:
        """Clear the heartbeat row if this worker holds it, then release the lock.

        The lock is released even if clearing the row raises, which propagates.
        """
        try:
            with Session(self._engine) as session:
                session.execute(
                    update(SchedulerLease)
                    .where(
                        SchedulerLease.id == SCHEDULER_LEASE_ID,
                        SchedulerLease.holder == self.holder,
                    )
                    .values(holder=None, heartbeat_at=None)
                    .execution_options(synchronize_session=False)
                )
                session.commit()
        finally:
            self._lock.release()
