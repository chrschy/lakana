"""Helpers shared by the integration modules of the server tests.

Names, git, lock waits, running a call in a thread, reading a task run, and the base of every hold.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 -- git with fixed argv in tests
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Connection, Engine, event, select, text
from sqlalchemy.orm import Session

from lakana_core.models import TaskRun

_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def unique_name() -> str:
    """Return a unique, dot-free name.

    Returns
    -------
    str
        e.g. ``t3f2a…``.
    """
    return f"t{uuid.uuid4().hex[:10]}"


def _git(cwd: Path, *args: str) -> str:
    """Run git with a fixed identity.

    Parameters
    ----------
    cwd
        Working directory.
    *args
        Arguments after ``git``.

    Returns
    -------
    str
        Stripped stdout.
    """
    return subprocess.run(  # nosec B603 B607 -- fixed argv in tests
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        env={**os.environ, **_IDENTITY},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_files(path: Path, files: dict[str, str]) -> str:
    """Write and commit files, creating the repository if needed.

    Parameters
    ----------
    path
        Repository directory.
    files
        Relative path → content.

    Returns
    -------
    str
        The new commit SHA.
    """
    if not (path / ".git").exists():
        path.mkdir(parents=True, exist_ok=True)
        _git(path, "init", "-q")
    for relative, content in files.items():
        (path / relative).parent.mkdir(parents=True, exist_ok=True)
        (path / relative).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--allow-empty", "-m", "change")
    return _git(path, "rev-parse", "HEAD")


def write_check(
    repository: Path, table: str, check: str, sql: str, *, on_failure: str | None = None
) -> Path:
    """Write a data quality check into a project, without committing it.

    Parameters
    ----------
    repository
        The project's directory.
    table
        The checked table's dotted name, e.g. ``silver.orders``.
    check
        The check's short name.
    sql
        The SELECT that returns the rows that are wrong.
    on_failure
        ``block`` or ``warn``; `None` writes no block (the default, ``block``, applies).

    Returns
    -------
    Path
        ``checks/<namespace>/<table>/<check>.sql`` in the project; `commit_files` commits it
        with whatever else changed.
    """
    path = repository.joinpath("checks", *table.split("."), f"{check}.sql")
    path.parent.mkdir(parents=True, exist_ok=True)
    block = "" if on_failure is None else f"/* lakana\non_failure: {on_failure}\n*/\n"
    path.write_text(f"{block}{sql}\n")
    return path


_PAUSE_LOCK = threading.Lock()
_PAUSES: list[StatementPause] = []
"""The pauses that are active, asked by `_pause_listener`."""
_LISTENING: set[int] = set()
"""The engines (by `id`) that already have `_pause_listener`."""


def _pause_listener(
    connection: Connection,
    cursor: object,
    statement: str,
    parameters: object,
    context: object,
    executemany: bool,
) -> None:
    """Hand a statement that is about to be sent to every active `StatementPause`.

    Parameters
    ----------
    connection
        The connection about to execute.
    cursor
        Its cursor.
    statement
        The SQL.
    parameters
        Its parameters.
    context
        The execution context.
    executemany
        Whether it is an `executemany`.
    """
    with _PAUSE_LOCK:
        active = tuple(_PAUSES)
    for pause in active:
        if pause.engine is connection.engine:
            pause.before(connection, statement)


class StatementPause:
    """Pauses one thread right before it sends its first statement that contains a text.

    The hold for a race between two sessions that needs no hook in the product: a
    `before_cursor_execute` listener on the engine that both sides use. The paused thread
    holds every lock its transaction has taken so far, and the statement it is about to send
    is the next thing the database sees from it. Use it as a context manager; leaving it
    releases the thread. The engine keeps one listener for good (`_pause_listener`), which
    asks the pauses that are active: removing a listener while another thread runs through
    the engine's listeners fails that thread.

    Parameters
    ----------
    db
        The engine whose connections the thread to pause uses.
    needle
        A part of the SQL as SQLAlchemy sends it, e.g. ``UPDATE task_runs SET written_table_id``.
    thread
        The name of the thread to pause; any other thread passes.
    timeout
        Seconds the paused thread waits to be released before it fails its own assertion.
    """

    def __init__(self, db: Engine, needle: str, thread: str, timeout: float = 30.0) -> None:
        self.reached = threading.Event()
        self.paused = threading.Event()
        self.release = threading.Event()
        self.backend: int | None = None
        self.engine = db
        self._db = db
        self._needle = needle
        self._thread = thread
        self._timeout = timeout

    def before(self, connection: Connection, statement: str) -> None:
        """Wait at the first matching statement of the named thread.

        Parameters
        ----------
        connection
            The connection about to execute.
        statement
            The SQL.
        """
        if (
            self.reached.is_set()
            or self._needle not in statement
            or threading.current_thread().name != self._thread
        ):
            return
        self.reached.set()  # first, so that the nested read below passes through
        self.backend = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        self.paused.set()
        assert self.release.wait(self._timeout), "the paused statement was never released"

    def __enter__(self) -> StatementPause:
        """Become active, installing the engine's listener if it has none yet.

        Returns
        -------
        StatementPause
            This pause.
        """
        with _PAUSE_LOCK:
            if id(self._db) not in _LISTENING:
                event.listen(self._db, "before_cursor_execute", _pause_listener)
                _LISTENING.add(id(self._db))
            _PAUSES.append(self)
        return self

    def __exit__(self, *exc: object) -> None:
        """Release the thread and stop being asked.

        Parameters
        ----------
        *exc
            The exception triple, ignored.
        """
        self.release.set()
        with _PAUSE_LOCK:
            _PAUSES.remove(self)

    def wait(self, timeout: float = 30.0) -> int:
        """Block until the thread is paused at its statement.

        Parameters
        ----------
        timeout
            Seconds to wait.

        Returns
        -------
        int
            The backend pid of the paused thread's connection, for `wait_for_lock_waiter`.

        Raises
        ------
        AssertionError
            If the thread never reached a matching statement.
        """
        assert self.paused.wait(timeout), f"no statement containing {self._needle!r} was sent"
        assert self.backend is not None
        return self.backend


def backend_pid(holder: Connection | Session) -> int:
    """Return the Postgres backend pid of a connection or session.

    Parameters
    ----------
    holder
        A connection or session with an open transaction.

    Returns
    -------
    int
        Its `pg_backend_pid()`.
    """
    return holder.execute(text("SELECT pg_backend_pid()")).scalar_one()


def wait_for_lock_waiter(db: Engine, blocker: int, timeout: float = 10.0) -> None:
    """Block until a backend waits on a lock that `blocker` holds.

    Matching the blocking pid, not any waiter: another test's or a
    background thread's lock wait must not satisfy this.

    Parameters
    ----------
    db
        The metadata database engine.
    blocker
        The backend pid of the transaction holding the lock (`backend_pid`).
    timeout
        Seconds to wait.

    Raises
    ------
    AssertionError
        If nothing waits on that backend in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db.connect() as connection:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND :blocker = ANY(pg_blocking_pids(pid))"
                ),
                {"blocker": blocker},
            ).scalar_one()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError(f"no backend is waiting on a lock held by backend {blocker}")


def run_in_thread(name: str, call: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    """Run a call in a named thread, keeping what it returns or raises.

    Parameters
    ----------
    name
        The thread's name; `StatementPause` and the holds pick a thread by it.
    call
        What to run; it opens its own session.

    Returns
    -------
    tuple
        The started thread and the list that receives the result or the exception.
    """
    results: list[object] = []

    def body() -> None:
        """Run the call, recording what comes out."""
        try:
            results.append(call())
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=body, name=name, daemon=True)
    thread.start()
    return thread, results


def thread_result(thread: threading.Thread, results: list[object], timeout: float = 60.0) -> object:
    """Join a thread of `run_in_thread` and return its one result.

    Parameters
    ----------
    thread
        The thread.
    results
        Its result list.
    timeout
        Seconds to wait for the thread to end.

    Returns
    -------
    object
        What the call returned or raised.
    """
    thread.join(timeout=timeout)
    assert not thread.is_alive(), f"{thread.name} did not end"
    (result,) = results
    return result


def task_row(db: Engine, run_id: str, asset: str = "silver.x") -> TaskRun:
    """Read a task run of a run as it is committed, detached.

    Parameters
    ----------
    db
        The metadata database engine.
    run_id
        The run.
    asset
        The task's asset.

    Returns
    -------
    TaskRun
        The row.
    """
    with Session(db) as session:
        row = session.scalars(
            select(TaskRun).where(TaskRun.run_id == uuid.UUID(run_id), TaskRun.asset == asset)
        ).one()
        session.expunge(row)
        return row


class HoldPoint:
    """The two events and the bounded wait that every hold of a test thread has.

    A hold wraps one call of the product so that a chosen thread stops there while the test acts
    on a second connection. The thread announces that it arrived (`reached`) and waits to be
    released (`release`); the wait is bounded, so a test that never releases fails the held
    thread instead of hanging.

    Parameters
    ----------
    timeout
        Seconds the held thread waits to be released before its own assertion fails.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self._timeout = timeout

    def wait_here(self) -> None:
        """Announce the hold and wait, bounded, to be released."""
        self.reached.set()
        assert self.release.wait(self._timeout), "the hold was never released"
