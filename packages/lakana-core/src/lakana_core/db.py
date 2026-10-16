"""Engine construction for the metadata database.

Every engine Lakana creates must come from here. SQLite does not enforce
declared foreign keys — including ``ondelete`` behaviour such as ``CASCADE``
and ``SET NULL`` — unless ``PRAGMA foreign_keys=ON`` is issued on each
connection, and ``lakana server start --local`` is a documented deployment
mode running on SQLite. Attaching the pragma here rather than in a test
fixture is what makes the cascade the catalog relies on real in that mode
and not only under pytest. A connection to a SQLite *file* (not the
in-memory database) also gets WAL journaling and a busy timeout, so local
mode's API and worker processes can share one database file instead of
racing each other with "database is locked" errors. Every engine also checks
a pooled connection before handing it out, so a connection that died while
idle is replaced and never fails the statement that happened to get it.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import sqlalchemy
from sqlalchemy import event, func, select
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session

from lakana_core.config import DatabaseConfig
from lakana_core.log_setup import LOG_FORMAT, SafeFormatter

_BUSY_TIMEOUT_MS = 5000
"""How long a SQLite connection waits for another process's lock (M4c local mode)."""


def _configure_sqlite(dbapi_connection: object, connection_record: object) -> None:
    """Configure a newly opened SQLite connection.

    Foreign keys are always enforced. A connection to a database *file* also
    gets WAL journaling and a busy timeout, so local mode's API and worker
    processes can share it without "database is locked" errors; an in-memory
    database is single-process and keeps its default journal mode. A
    read-only file keeps its existing journal mode too: switching to WAL
    writes to the file, which a read-only open cannot do, so that pragma is
    best-effort and reads still work without it.

    Parameters
    ----------
    dbapi_connection
        The raw DBAPI connection just established.
    connection_record
        SQLAlchemy's pool record for the connection. Unused.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    database = cursor.execute("PRAGMA database_list").fetchone()[2]
    if database:
        # A read-only file (e.g. chmod 0400) can't take the WAL switch, which
        # writes to the file; reads must still work without it.
        with contextlib.suppress(sqlite3.OperationalError):
            cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


DEFAULT_LEASE_SECONDS = 60
"""The lease the keepalive values derive from when a caller does not pass one."""

logger = logging.getLogger(__name__)


def keepalive_settings(lease_seconds: int) -> dict[str, int]:
    """Derive the PostgreSQL TCP settings that make the server notice a dead client.

    The server probes the client after ``idle`` seconds of silence, then every
    ``interval`` seconds, and drops the backend after ``count`` unanswered
    probes. `tcp_user_timeout` (milliseconds) bounds the same wait when the
    server has unacknowledged data to send, which keepalives do not cover; it
    is set to what the probes need, because on Linux it replaces the probe
    count as the rule that closes a silent connection. With the default lease
    of 60 s the values are 20 s, 10 s, 2 probes and 40 s: a dead client is
    noticed within two thirds of a lease, so the advisory lock of a vanished
    scheduler leader is freed before its lease would have run out twice.

    The values have limits. The lease has no upper bound in the configuration; above about
    98,000 s the kernel rejects the idle value silently. For a lease under 6 s the minimum of
    1 s per value makes detection take 3 s or more. A client that is alive can be cut in one
    case: it is frozen for longer than the user timeout while a result larger than the socket
    buffers is in transit to it (the peer's window stays zero, so nothing is acknowledged).

    Parameters
    ----------
    lease_seconds
        The task lease (`LAKANA_RUN__LEASE_SECONDS`).

    Returns
    -------
    dict of str to int
        PostgreSQL setting names to values, in the order they are issued.
    """
    idle = max(1, lease_seconds // 3)
    interval = max(1, lease_seconds // 6)
    count = 2
    return {
        "tcp_keepalives_idle": idle,
        "tcp_keepalives_interval": interval,
        "tcp_keepalives_count": count,
        "tcp_user_timeout": (idle + count * interval) * 1000,
    }


def _keepalive_listener(lease_seconds: int) -> Callable[[DBAPIConnection, object], None]:
    """Build the connect listener that sets the keepalive settings on a PostgreSQL session.

    A server that rejects a setting (a managed PostgreSQL, or a platform without
    the socket option) costs one warning per engine and never the connection:
    each setting is issued and committed on its own, so one refusal neither
    undoes the others nor leaves a transaction open.

    Parameters
    ----------
    lease_seconds
        The task lease the values derive from.

    Returns
    -------
    Callable
        The listener, with the signature SQLAlchemy's connect event expects.
    """
    settings = keepalive_settings(lease_seconds)
    warned: list[bool] = []

    def _set_keepalives(dbapi_connection: DBAPIConnection, connection_record: object) -> None:
        """Issue the settings on a newly opened connection.

        Parameters
        ----------
        dbapi_connection
            The raw DBAPI connection just established.
        connection_record
            SQLAlchemy's pool record for the connection. Unused.
        """
        refused: list[str] = []
        for name, value in settings.items():
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"SET {name} = {int(value)}")
                dbapi_connection.commit()
            except Exception:
                refused.append(name)
                with contextlib.suppress(Exception):
                    dbapi_connection.rollback()
            finally:
                with contextlib.suppress(Exception):
                    cursor.close()
        if refused and not warned:
            warned.append(True)
            logger.warning(
                "The database refused %s on a connection. Without them PostgreSQL may keep the "
                "database session of a scheduler leader that vanished (power loss, a cut "
                "network) for hours, and nobody ends the runs it held until it does. "
                "Lakana keeps running; to recover, end that session in PostgreSQL "
                "(`lakana scheduler status` names the worker).",
                ", ".join(refused),
            )

    return _set_keepalives


def create_engine(config: DatabaseConfig, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Engine:
    """Create an engine with Lakana's connection-level guarantees attached.

    The pragma listener is registered against *this* engine rather than
    against SQLAlchemy's `Engine` class, so importing `lakana_core` does not
    silently change the behaviour of engines the host application created
    for its own databases.

    Every connection is checked when it leaves the pool (`pool_pre_ping`) and
    replaced if the database or a firewall closed it while it sat idle. A
    worker supervising a long Python task comes back to connections that have
    been idle for as long as the task ran; without the check, the first
    statement after the task (revoking the task's token, recording the
    outcome) would fail on a dead connection.

    Parameters
    ----------
    config
        Database URL and echo flag.
    lease_seconds
        The task lease (`LAKANA_RUN__LEASE_SECONDS`). On PostgreSQL every connection
        of the engine asks the server to notice a vanished client within two thirds of
        it (`keepalive_settings`); SQLite ignores it.

    Returns
    -------
    Engine
        A configured engine. The caller owns it and must dispose of it.
    """
    echo_logger = logging.getLogger("sqlalchemy.engine.Engine")
    before = list(echo_logger.handlers)
    engine = sqlalchemy.create_engine(config.url, echo=config.echo, pool_pre_ping=True)
    event.listen(engine, "connect", _configure_sqlite)
    if engine.dialect.name == "postgresql":
        event.listen(engine, "connect", _keepalive_listener(lease_seconds))
    if config.echo:  # echo adds a handler of its own, which would bypass the escaping
        for handler in echo_logger.handlers:
            if handler not in before:  # only the one SQLAlchemy just added, not an embedder's
                handler.setFormatter(SafeFormatter(LOG_FORMAT))
    return engine


def rowcount(result: object) -> int:
    """Return the number of rows a DML statement matched.

    The one place that narrows `Session.execute`'s untyped result to a
    `CursorResult`, so compare-and-swap call sites check `rowcount != 1`
    without each repeating the cast.

    Parameters
    ----------
    result
        What `Session.execute` or `Connection.execute` returned for an
        ``UPDATE`` or ``DELETE``.

    Returns
    -------
    int
        Rows matched.
    """
    return cast("CursorResult[Any]", result).rowcount


def _as_utc(value: object) -> datetime:
    """Normalise what a driver returns for ``CURRENT_TIMESTAMP`` to aware UTC.

    Parameters
    ----------
    value
        SQLite returns a naive-UTC ``"YYYY-MM-DD HH:MM:SS"`` string; a driver
        may also return a naive datetime (UTC by SQLite's contract); Postgres
        returns an aware datetime in the session's time zone.

    Returns
    -------
    datetime
        The instant, timezone-aware, in UTC.

    Raises
    ------
    TypeError
        If `value` is none of those shapes.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError(f"unexpected CURRENT_TIMESTAMP value: {value!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def db_now(session: Session) -> datetime:
    """Return the database server's current time.

    Lease and heartbeat times are the database's, never a worker's wall clock,
    so clock skew between workers cannot expire a live lease or keep a dead one.
    SQLite's ``CURRENT_TIMESTAMP`` has one-second resolution.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    datetime
        Aware UTC time.
    """
    return _as_utc(session.execute(select(func.current_timestamp())).scalar_one())
