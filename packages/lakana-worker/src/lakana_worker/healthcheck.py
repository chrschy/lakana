"""`lakana-worker healthcheck`: whether the worker of this host reaches the database.

A container's healthcheck runs this next to the worker. It reads the same settings, asks the
database once whether a worker of this host (`socket.gethostname()`, the host the worker
registers with) has written a heartbeat within `LAKANA_RUN__LEASE_SECONDS`, and exits 0 if one
has, 1 if not. So it fails both when this check cannot reach the database and when the worker
itself cannot: a worker writes its heartbeat every `LAKANA_RUN__RENEW_SECONDS` only while it
reaches the database. It writes nothing, starts no worker thread and takes at most
`HEALTHCHECK_TIMEOUT_SECONDS`.
"""

from __future__ import annotations

import socket
import sys
import threading
from collections.abc import Callable

from pydantic import ValidationError
from sqlalchemy.orm import Session

from lakana_core.config import Settings
from lakana_core.db import create_engine, db_now
from lakana_core.identifiers import shown
from lakana_core.runs.workers import alive_on_host

HEALTHCHECK_TIMEOUT_SECONDS = 3.0
"""The longest the check waits for the database; below the bundle's healthcheck timeout (5 s)."""


def check_health(settings: Settings, host: str) -> str | None:
    """Ask the database whether a worker of this host has a fresh heartbeat.

    Parameters
    ----------
    settings
        The worker's settings: the database and the lease.
    host
        This host's name.

    Returns
    -------
    str or None
        `None` when healthy; otherwise what is wrong and what to do, without the database's
        own error text (it can hold a host or a password).
    """
    lease = settings.run.lease_seconds
    engine = create_engine(settings.database, lease_seconds=lease)
    try:
        with Session(engine) as session:
            alive = alive_on_host(session, host, db_now(session), lease)
    except Exception as exc:
        return (
            f"The database could not be reached ({type(exc).__name__}). Check "
            "LAKANA_DATABASE__URL and that the database is up; the worker's log says more."
        )
    finally:
        engine.dispose()
    if alive:
        return None
    return (
        f"No worker on this host ({shown(host)}) has written a heartbeat in the last {lease} s "
        "(LAKANA_RUN__LEASE_SECONDS): it cannot reach the database, or it has not started yet. "
        "Read the worker's log."
    )


def run_healthcheck(
    check: Callable[[], str | None], timeout: float = HEALTHCHECK_TIMEOUT_SECONDS
) -> int:
    """Run a check in a daemon thread for at most `timeout` seconds and print its outcome.

    Parameters
    ----------
    check
        The check; returns `None` when healthy, else the message.
    timeout
        Seconds to wait. A check that has not answered by then fails; its thread is a daemon,
        so the process ends anyway.

    Returns
    -------
    int
        The exit code: 0 healthy, 1 not.
    """
    outcome: list[str | None] = []

    def run() -> None:
        """Run the check and keep what it says."""
        try:
            outcome.append(check())
        except Exception as exc:
            outcome.append(f"The health check failed unexpectedly ({type(exc).__name__}).")

    thread = threading.Thread(target=run, name="lakana-healthcheck", daemon=True)
    thread.start()
    thread.join(timeout)
    if not outcome:
        print(
            f"The database did not answer within {timeout:g} s. Check that it is up and "
            "reachable from this container; the worker's log says more.",
            file=sys.stderr,
        )
        return 1
    message = outcome[0]
    if message is not None:
        print(message, file=sys.stderr)
        return 1
    print("healthy: the worker of this host has a fresh heartbeat.")
    return 0


def healthcheck() -> int:
    """Read the settings and check the worker of this host.

    Returns
    -------
    int
        The exit code: 0 healthy, 1 not (also when the settings cannot be read).
    """
    try:
        settings = Settings()
    except ValidationError as exc:
        wrong = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        print(
            f"The worker's settings could not be read ({wrong}); the worker's own start-up "
            "message says how to fix them.",
            file=sys.stderr,
        )
        return 1
    host = socket.gethostname()
    return run_healthcheck(lambda: check_health(settings, host))
