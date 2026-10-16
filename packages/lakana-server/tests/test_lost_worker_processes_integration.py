"""With real worker processes: a killed worker's work ends, a queued query says why.

Each test starts `python -m lakana_worker` as a child of the test, in a session of its own, with
a lease of 3 seconds, and kills it for real (`SIGKILL` to its process group). What the unit tier
and the thread-based module (`test_lost_workers_integration.py`) cannot show is here: a process
that vanishes without a word, the OS closing its database connections, and a surviving worker
taking over the scheduler role and sweeping.

Every time bound is computed from the settings the tests use, as the sweep needs them: a lost
run is ended within the lease (the worker counts as lost), plus the renew interval and 5 seconds
(a new leader waits for the heartbeats of the others before it sweeps), plus one scheduler
interval, plus a margin for a loaded machine. No sleep decides a pass: every wait polls a
condition with a bound.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import subprocess  # nosec B404 -- the worker under test, fixed argv
import sys
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lakana.errors import QueryFailed
from lakana.resources.queries import QueryRun
from lakana_core.catalog.service import Rollback
from lakana_core.config import RunConfig
from lakana_core.db import create_engine, db_now
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import (
    AttemptOutcome,
    QueryRunStatus,
    RollbackOutcome,
    TaskAttempt,
    TaskRun,
    TaskStatus,
)
from lakana_core.models import QueryRun as TaskQueryRun
from lakana_core.runs.checks import (
    ROLLBACK_SENTENCES,
    RUN_CANCELLED_MESSAGE,
    SET_BACK_DONE,
    results_of_task,
    set_back_ending,
    verdict,
)
from lakana_core.runs.checks import failure_message as check_failure_message
from lakana_core.runs.store import (
    CHECK_LOST_MESSAGE,
    QUERY_LOST_MESSAGE,
    WORKER_LOST_WRITE_MESSAGE,
    scheduler_tick,
)
from lakana_worker.deploy import DeploySettings
from lakana_worker.poller import poll_once
from lakana_worker.scheduler import SchedulerLoop
from tests.integration_helpers import commit_files, task_row, unique_name, write_check
from tests.python_asset_helpers import (
    IDS,
    MASTER_KEY,
    QueryStack,
    completions,
    long_sql,
    query_stack,
    until,
)

pytestmark = pytest.mark.integration

LEASE = 3
"""`LAKANA_RUN__LEASE_SECONDS` of the server and every worker of this module."""

RENEW = 1
"""`LAKANA_RUN__RENEW_SECONDS`."""

INTERVAL = 0.5
"""`LAKANA_RUN__SCHEDULER_INTERVAL_SECONDS`."""

RETRY = 5
"""The seconds a leader adds to the renew interval before it sweeps (`heartbeat.RETRY_SECONDS`)."""

MARGIN = 20.0
"""Seconds added to every computed bound, for a loaded machine."""

ENDED_WITHIN = LEASE + RENEW + RETRY + INTERVAL + MARGIN
"""The bound for a lost run to end once its worker is gone (R9.1)."""

START_WITHIN = 30.0
"""The bound for a worker process to register."""


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[QueryStack]:
    """Start Postgres, SeaweedFS and a real HTTP server whose lease is `LEASE` seconds.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    QueryStack
        The stack.
    """
    run = RunConfig(
        lease_seconds=LEASE,
        renew_seconds=RENEW,
        scheduler_interval_seconds=INTERVAL,
        sweep_interval=0,
    )
    with query_stack(tmp_path_factory, lambda s: s.model_copy(update={"run": run})) as built:
        yield built


@dataclass(frozen=True)
class WorkerProcess:
    """A running worker process.

    Parameters
    ----------
    process
        The child; its pid is also its process group's id.
    id
        The worker's id in the `workers` table.
    log
        The file the worker writes its log to.
    """

    process: subprocess.Popen[bytes]
    id: str
    log: Path

    def kill(self) -> None:
        """Send `SIGKILL` to the worker's process group, reap the worker, and wait for the group.

        Raises
        ------
        AssertionError
            If a process of the group is still alive after the bound.
        """
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=30)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        raise AssertionError("a process of the killed worker's group is still alive")


def _environment(stack: QueryStack, home: Path, takes: str | None = None) -> dict[str, str]:
    """Build the worker's environment from scratch.

    Parameters
    ----------
    stack
        The stack; the database comes from its settings.
    home
        A private home directory (the worker's caches go below it).
    takes
        The value of `LAKANA_WORKER__TAKES`; `None` leaves the setting out (the worker takes all).

    Returns
    -------
    dict
        Only what the worker needs: nothing of the test process's own environment leaks in.
    """
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LAKANA_DATABASE__URL": stack.services.settings.database.url,
        "LAKANA_SECRETS__MASTER_KEY": MASTER_KEY,
        "LAKANA_RUN__LEASE_SECONDS": str(LEASE),
        "LAKANA_RUN__RENEW_SECONDS": str(RENEW),
        "LAKANA_RUN__SCHEDULER_INTERVAL_SECONDS": str(INTERVAL),
        "LAKANA_RUN__SWEEP_INTERVAL": "0",
        "LAKANA_RUN__DRAIN_GRACE_SECONDS": "2",
        "LAKANA_WORKER__MEMORY_LIMIT": "2GB",
    }
    if takes is not None:
        environment["LAKANA_WORKER__TAKES"] = takes
    return environment


def _registered(stack: QueryStack, pid: int) -> str | None:
    """Find the id of the worker with this pid in the `workers` table.

    Parameters
    ----------
    stack
        The stack.
    pid
        The worker's process id (the middle part of its id).

    Returns
    -------
    str or None
        The id, or `None` when the worker has not registered.
    """
    with Session(stack.db) as session:
        return session.execute(
            text("SELECT id FROM workers WHERE id LIKE :pattern"), {"pattern": f"%:{pid}:%"}
        ).scalar_one_or_none()


@contextmanager
def _worker_process(
    stack: QueryStack, home: Path, *, takes: str | None = None
) -> Generator[WorkerProcess, None, None]:
    """Start a worker process, wait until it registered, and kill it at the end whatever happened.

    Parameters
    ----------
    stack
        The stack.
    home
        A private home directory for this worker.
    takes
        The value of `LAKANA_WORKER__TAKES` for the child; `None` leaves it to the default.

    Yields
    ------
    WorkerProcess
        The worker.

    Raises
    ------
    AssertionError
        If the worker does not register within `START_WITHIN` seconds or ends early; the
        message carries the tail of its log.
    """
    home.mkdir(parents=True, exist_ok=True)
    log = home / "worker.log"
    with log.open("wb") as sink:
        process = subprocess.Popen(  # nosec B603 -- fixed argv, environment built here
            [sys.executable, "-m", "lakana_worker"],
            env=_environment(stack, home, takes),
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + START_WITHIN
        worker_id = _registered(stack, process.pid)
        while worker_id is None:
            tail = log.read_text(errors="replace")[-2000:]
            assert process.poll() is None, (
                f"the worker ended at start ({process.returncode}): {tail}"
            )
            assert time.monotonic() < deadline, f"the worker did not register: {tail}"
            time.sleep(0.1)
            worker_id = _registered(stack, process.pid)
        yield WorkerProcess(process, worker_id, log)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=30)


def _worker_of(stack: QueryStack, run_id: str) -> str | None:
    """Read the worker id a run carries.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run's id.

    Returns
    -------
    str or None
        The id, `None` when the run has none.
    """
    return stack.row(run_id).worker_id


def _until_running_under(stack: QueryStack, run_id: str, worker_id: str) -> None:
    """Wait, bounded, until a run is `RUNNING` under a worker.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run's id.
    worker_id
        The worker's id.
    """
    until(lambda: str(_worker_of(stack, run_id)), {worker_id}, START_WITHIN)
    until(lambda: stack.row(run_id).status.value, {"RUNNING"}, START_WITHIN)


def _tick_until_ended(stack: QueryStack, run_id: str) -> str:
    """Run the scheduler's tick every 0.2 s, as the leader would, until the run has ended.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run's id.

    Returns
    -------
    str
        The run's final status.
    """
    rand = secrets.SystemRandom().random

    def tick() -> str:
        """Tick once and read the run's status.

        Returns
        -------
        str
            The status.
        """
        with Session(stack.db) as session:
            scheduler_tick(session, db_now(session), rand, lease_seconds=LEASE)
        time.sleep(0.2)
        return stack.row(run_id).status.value

    return until(tick, {"SUCCEEDED", "FAILED", "CANCELLED"}, ENDED_WITHIN)


def _workers_status(stack: QueryStack, worker_id: str) -> str:
    """Read a worker's status (`alive`, `lost` or `gone`) from the admin listing.

    Parameters
    ----------
    stack
        The stack.
    worker_id
        The worker's id.

    Returns
    -------
    str
        The status, `gone` when it is not listed.
    """
    for info in stack.admin.workers.list():
        if info.id == worker_id:
            return info.status
    return "gone"


def test_a_query_of_a_killed_worker_ends_failed_as_worker_lost(
    stack: QueryStack, tmp_path: Path
) -> None:
    environment = stack.environment()
    with _worker_process(stack, tmp_path / "w") as worker:
        run = stack.admin.queries.submit(long_sql("killed"), environment)
        _until_running_under(stack, run.id, worker.id)
        worker.kill()  # also: no process of its group (its query child) is left
    assert stack.row(run.id).status.value == "RUNNING"  # nobody has swept yet
    assert _tick_until_ended(stack, run.id) == "FAILED"
    status = run.status()
    assert (status.status, status.error_cause, status.error_message) == (
        "FAILED",
        "worker_lost",
        QUERY_LOST_MESSAGE,
    )
    with pytest.raises(QueryFailed) as failed:
        run.wait(timeout=10)
    assert failed.value.cause == "worker_lost"
    assert completions(stack, run.id) == ["FAILED"]  # exactly one event


def test_a_cancel_of_a_killed_workers_query_answers_cancelled_at_once(
    stack: QueryStack, tmp_path: Path
) -> None:
    environment = stack.environment()
    with _worker_process(stack, tmp_path / "w") as worker:
        run = stack.admin.queries.submit(long_sql("cancelled"), environment)
        _until_running_under(stack, run.id, worker.id)
        worker.kill()
    until(lambda: _workers_status(stack, worker.id), {"lost"}, LEASE + MARGIN)
    assert stack.row(run.id).status.value == "RUNNING"  # no sweep has run
    assert run.cancel().status == "CANCELLED"  # the answer itself, not a later poll
    assert stack.row(run.id).status.value == "CANCELLED"
    assert completions(stack, run.id) == ["CANCELLED"]


def test_the_surviving_worker_takes_the_role_and_ends_the_dead_ones_query(
    stack: QueryStack, tmp_path: Path
) -> None:
    environment = stack.environment()
    with (
        _worker_process(stack, tmp_path / "a") as first,
        _worker_process(stack, tmp_path / "b") as second,
    ):
        ids = {first.id, second.id}
        holder = until(lambda: stack.admin.scheduler.status().holder or "", ids, START_WITHIN)
        leader, other = (first, second) if holder == first.id else (second, first)
        # The query must run on the leader: the other worker is stopped (not killed) while
        # the leader claims, so there is nothing to retry.
        os.killpg(other.process.pid, signal.SIGSTOP)
        try:
            run = stack.admin.queries.submit(long_sql("failover"), environment)
            _until_running_under(stack, run.id, leader.id)
        finally:
            os.killpg(other.process.pid, signal.SIGCONT)
        leader.kill()
        status = until(lambda: run.status().status, {"FAILED", "SUCCEEDED"}, ENDED_WITHIN)
        assert status == "FAILED"
        final = run.status()
        assert (final.error_cause, final.error_message) == ("worker_lost", QUERY_LOST_MESSAGE)
        until(lambda: stack.admin.scheduler.status().holder or "", {other.id}, ENDED_WITHIN)
        assert completions(stack, run.id) == ["FAILED"]


def test_a_cleanly_stopped_worker_removes_its_row(stack: QueryStack, tmp_path: Path) -> None:
    with _worker_process(stack, tmp_path / "w") as worker:
        assert _workers_status(stack, worker.id) == "alive"
        os.killpg(worker.process.pid, signal.SIGTERM)
        assert worker.process.wait(timeout=START_WITHIN) == 0, worker.log.read_text()[-2000:]
    assert _workers_status(stack, worker.id) == "gone"


def test_a_queued_query_says_no_worker_and_then_starts(stack: QueryStack, tmp_path: Path) -> None:
    environment = stack.environment()
    run = stack.admin.queries.submit("SELECT 41 + 1 AS answer", environment)

    def code() -> str:
        """Read the code of the query's wait reason.

        Returns
        -------
        str
            The code, empty when there is none.
        """
        reason = run.status().waiting_for
        return reason.code if reason is not None else ""

    until(code, {"no_worker"}, LEASE + MARGIN)  # earlier tests' workers are dead or stopped
    with _worker_process(stack, tmp_path / "w"):
        status = run.wait(timeout=START_WITHIN)
    assert status.status == "SUCCEEDED"
    assert run.result().to_pylist() == [{"answer": 42}]


def _finished(stack: QueryStack, run_id: str) -> datetime:
    """Read when a run was ended (the sweep stamps its tick's database time).

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run's id.

    Returns
    -------
    datetime
        The run's `finished_at`.
    """
    finished = stack.row(run_id).finished_at
    assert finished is not None
    return finished


def _ghost_run(stack: QueryStack, environment: str, tag: str) -> QueryRun:
    """Make a query that is `RUNNING` under a worker that does not exist, as a lost one is.

    Parameters
    ----------
    stack
        The stack.
    environment
        The environment.
    tag
        Names the result column.

    Returns
    -------
    lakana.QueryRun
        The handle; no worker will take it, because it is no longer queued.
    """
    run = stack.admin.queries.submit(f"SELECT 1 AS {tag}", environment)
    with Session(stack.db) as session:
        session.execute(
            text(
                "UPDATE query_runs SET status = 'RUNNING', worker_id = 'ghost:1:00000000', "
                "started_at = now() WHERE id = :id AND status = 'QUEUED'"
            ),
            {"id": run.id},
        )
        session.commit()
    return run


def test_a_new_leader_and_a_stalled_leader_wait_for_the_heartbeats_before_they_sweep(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Postgres, with the real loop: the pause of a new leader and of a leader that stalled."""
    environment = stack.environment()
    engine = create_engine(stack.services.settings.database)
    first = _ghost_run(stack, environment, "first")
    entered, release = threading.Event(), threading.Event()
    stall = threading.Event()
    real_now = SchedulerLoop._now

    def now(loop: SchedulerLoop) -> datetime:
        """Hold the start of one step while `stall` is set, as a suspended or overloaded leader.

        The step reads the database's time only after the hold, so the gap to its previous tick
        is the length of the stall.

        Parameters
        ----------
        loop
            The scheduler loop.

        Returns
        -------
        datetime
            What the real clock returns after the hold.
        """
        if stall.is_set() and not entered.is_set():
            entered.set()
            assert release.wait(60), "the stalled step was never released"
        return real_now(loop)

    monkeypatch.setattr(SchedulerLoop, "_now", now)
    loop = SchedulerLoop(
        engine,
        "pause-test-leader",
        interval=INTERVAL,
        lease_seconds=LEASE,
        renew_seconds=RENEW,
    )
    with Session(stack.db) as session:
        started = db_now(session)
    thread = loop.start()
    try:
        until(lambda: stack.row(first.id).status.value, {"FAILED"}, ENDED_WITHIN)
        pause = RENEW + RETRY
        # A new leader waits renew + 5 s of the database's time from its first tick.
        assert (_finished(stack, first.id) - started).total_seconds() >= pause
        stall.set()
        until(lambda: "entered" if entered.is_set() else "", {"entered"}, ENDED_WITHIN)
        second = _ghost_run(stack, environment, "second")
        with Session(stack.db) as session:
            held_from = db_now(session)
        # Held for more than half a lease plus the interval, by the database's clock.
        deadline = time.monotonic() + ENDED_WITHIN
        while True:
            with Session(stack.db) as session:
                held = (db_now(session) - held_from).total_seconds()
            if held > LEASE / 2 + INTERVAL + 1:
                break
            assert time.monotonic() < deadline
            time.sleep(0.1)
        with Session(stack.db) as session:
            released_at = db_now(session)
        release.set()
        until(lambda: stack.row(second.id).status.value, {"FAILED"}, ENDED_WITHIN)
        waited = (_finished(stack, second.id) - released_at).total_seconds()
        assert waited >= pause, f"the first tick after a stall swept after only {waited:.1f} s"
    finally:
        release.set()
        loop.stop()
        thread.join(timeout=30)
        engine.dispose()
    assert not thread.is_alive()
    assert completions(stack, first.id) == ["FAILED"]
    assert completions(stack, second.id) == ["FAILED"]


QUERY_SECONDS = 15.0
"""A bound for `long_sql` (about 9 s by itself) on a loaded machine; not derived from a setting."""


def _code_of(run: QueryRun) -> Callable[[], str]:
    """Build a reader of the code of a query's wait reason.

    Parameters
    ----------
    run
        The query.

    Returns
    -------
    callable
        Returns the code, empty when the query has no wait reason.
    """

    def code() -> str:
        """Read the code of the query's wait reason.

        Returns
        -------
        str
            The code, empty when there is none.
        """
        reason = run.status().waiting_for
        return reason.code if reason is not None else ""

    return code


def _until_no_worker_alive(stack: QueryStack) -> None:
    """Wait, bounded, until the workers of earlier tests are no longer alive.

    A killed worker counts as alive until its lease ends, and the number of workers decides what
    `auto` allows and which reason a query shows.

    Parameters
    ----------
    stack
        The stack.
    """

    def alive() -> str:
        """Count the workers that are alive.

        Returns
        -------
        str
            `none` when no worker is alive, otherwise `some`.
        """
        return "some" if any(w.status == "alive" for w in stack.admin.workers.list()) else "none"

    until(alive, {"none"}, LEASE + MARGIN)


def test_a_query_waits_for_a_worker_of_its_kind_and_then_starts(
    stack: QueryStack, tmp_path: Path
) -> None:
    _until_no_worker_alive(stack)
    environment = stack.environment()
    with _worker_process(stack, tmp_path / "pipeline", takes="pipeline") as pipeline_worker:
        listed = {w.id: w for w in stack.admin.workers.list()}
        assert listed[pipeline_worker.id].takes == "pipeline"
        run = stack.admin.queries.submit("SELECT 41 + 1 AS answer", environment)
        beat = listed[pipeline_worker.id].heartbeat_at

        def beats() -> str:
            """Say whether the pipeline worker's heartbeat has advanced since the submit.

            Returns
            -------
            str
                `advanced` or `same`.
            """
            now = {w.id: w for w in stack.admin.workers.list()}[pipeline_worker.id].heartbeat_at
            return "advanced" if now > beat else "same"

        until(_code_of(run), {"no_worker_of_kind"}, LEASE + MARGIN)
        # At least one renew interval has passed; the last assertion proves who ran it.
        until(beats, {"advanced"}, LEASE + MARGIN)
        waiting = run.status()
        assert waiting.status == "QUEUED"
        assert waiting.waiting_for is not None
        assert waiting.waiting_for.other_workers == 1
        assert waiting.waiting_for.message == (
            "No worker that takes interactive work is running (1 worker takes pipeline work "
            "only), so this query cannot start. Ask the operator to start a worker with "
            "LAKANA_WORKER__TAKES=interactive or all."
        )
        with _worker_process(stack, tmp_path / "interactive", takes="interactive") as taker:
            status = run.wait(timeout=START_WITHIN)
            assert status.status == "SUCCEEDED"
            assert run.result().to_pylist() == [{"answer": 42}]
            assert _worker_of(stack, run.id) == taker.id


def test_a_third_query_of_one_user_waits_while_a_worker_is_free_and_starts_when_one_ends(
    stack: QueryStack, tmp_path: Path
) -> None:
    _until_no_worker_alive(stack)
    environment = stack.environment()
    with contextlib.ExitStack() as workers:
        for name in ("a", "b", "c"):
            workers.enter_context(_worker_process(stack, tmp_path / name, takes="interactive"))
        runs = [stack.admin.queries.submit(long_sql(f"q{i}"), environment) for i in range(3)]
        try:

            def running() -> str:
                """Say how many of the three queries are running.

                Returns
                -------
                str
                    The count as text.
                """
                return str(sum(stack.row(r.id).status.value == "RUNNING" for r in runs))

            until(running, {"2"}, START_WITHIN)
            held = next(r for r in runs if stack.row(r.id).status.value == "QUEUED")
            until(_code_of(held), {"user_limit"}, START_WITHIN)
            reason = held.status().waiting_for
            assert reason is not None
            assert (reason.running, reason.limit) == (2, 2)
            assert running() == "2"  # the third worker was free the whole time
            first = next(r for r in runs if r is not held)
            first.cancel()
            until(lambda: stack.row(held.id).status.value, {"RUNNING", "SUCCEEDED"}, QUERY_SECONDS)
            assert _worker_of(stack, held.id) is not None
        finally:
            for run in runs:
                with contextlib.suppress(Exception):
                    run.cancel()


COUNT = 15.0
"""The bound for the module's long query to end by itself (it runs about nine seconds)."""


def _task_query_runs(stack: QueryStack, task_id: uuid.UUID) -> list[TaskQueryRun]:
    """Read a task's own query runs (its write and the counts of its checks), oldest first.

    Parameters
    ----------
    stack
        The stack.
    task_id
        The task run.

    Returns
    -------
    list of QueryRun rows
        Detached.
    """
    with Session(stack.db) as session:
        rows = list(
            session.scalars(
                select(TaskQueryRun)
                .where(TaskQueryRun.task_run_id == task_id)
                .order_by(TaskQueryRun.created_at, TaskQueryRun.id)
            )
        )
        session.expunge_all()
        return rows


def _tick_until(
    stack: QueryStack, run_id: str, reached: Callable[[TaskRun], bool], seconds: float
) -> TaskRun:
    """Tick every 0.2 s, as the leader would, until the checked task is where the test wants it.

    At every poll the task must not be `succeeded` and the task below it must not have become
    ready: that is what the test is about, and a state that lasted one tick would be missed
    by a single look at the end.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run.
    reached
        Says whether the wait is over, from the task run of ``silver.x``.
    seconds
        The bound.

    Returns
    -------
    TaskRun
        The task run of ``silver.x`` when `reached` said yes.
    """
    rand = secrets.SystemRandom().random
    deadline = time.monotonic() + seconds
    while True:
        with Session(stack.db) as session:
            scheduler_tick(session, db_now(session), rand, lease_seconds=LEASE)
        task = task_row(stack.db, run_id, "silver.x")
        below = task_row(stack.db, run_id, "gold.y")
        assert task.status is not TaskStatus.SUCCEEDED, "the unchecked task counted as succeeded"
        assert below.status in {TaskStatus.PENDING, TaskStatus.UPSTREAM_FAILED}, below.status
        assert below.ready_at is None
        if reached(task):
            return task
        assert time.monotonic() < deadline, f"still {task.status} after {seconds:g} s"
        time.sleep(0.2)


def test_a_worker_killed_between_write_and_checks_is_checked_by_the_next_claim(
    stack: QueryStack, tmp_path: Path
) -> None:
    """Review focus 2: a real worker process is killed while it counts its first check.

    The table is written and the task run stamped. The first check runs about nine seconds
    and then passes; the second fails. The killed worker's attempt is `lost`, the task is
    never `succeeded`, nobody builds the table a second time, and the worker that claims the
    task next runs the checks, fails the task and sets the table back.
    """
    environment, repository = stack.environment(), unique_name()
    stack.admin.tables.create(environment, "silver.x", schema=IDS)
    before = stack.admin.tables.append(
        environment, "silver.x", pa.table({"id": pa.array([7, 8], pa.int64())})
    ).snapshot_id
    path = stack.services.roots / repository
    slow = f"SELECT x.id FROM silver.x AS x, ({long_sql('n')}) AS q WHERE q.n IS NULL"
    write_check(path, "silver.x", "a_slow", slow)
    write_check(path, "silver.x", "b_small", "SELECT id FROM silver.x WHERE id > 2")
    commit_files(
        path,
        {
            "lakana.yaml": "defaults:\n  retry_delay: 0\n",
            "assets/silver/x.sql": "SELECT id FROM bronze.nums\n",
            "assets/gold/y.sql": "SELECT count(*) AS n FROM silver.x\n",
        },
    )
    stack.admin.repositories.create(repository, "local", str(path))
    deploy = stack.admin.deploys.create(environment, repository)
    # The deploy runs in this process: the worker processes below have no repository root.
    settings = DeploySettings(repo_roots=(str(stack.services.roots),))
    with Session(stack.db) as session:
        assert poll_once(session, GrantAuthorization(session), deploy=settings)
    stack.admin.deploys.wait(environment, deploy.id, timeout=30)

    def counting(task: TaskRun) -> bool:
        """Say whether the task is stamped and one of its counts is running.

        Parameters
        ----------
        task
            The task run.

        Returns
        -------
        bool
            Whether the kill would land between the write and the verdict.
        """
        return task.written_table_id is not None and any(
            run.write_mode is None and run.status is QueryRunStatus.RUNNING
            for run in _task_query_runs(stack, task.id)
        )

    with _worker_process(stack, tmp_path / "first") as first:
        run = stack.admin.runs.start(environment, repository)
        task = _tick_until(stack, run.id, counting, START_WITHIN + COUNT)
        assert (task.status, task.lease_owner, task.attempt) == (TaskStatus.RUNNING, first.id, 1)
        first.kill()
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id != before  # unchecked data

    with _worker_process(stack, tmp_path / "second") as second:
        done = _tick_until(
            stack,
            run.id,
            lambda task: task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED},
            ENDED_WITHIN + START_WITHIN + 2 * COUNT,
        )
        finished = until(
            lambda: stack.admin.runs.get(environment, run.id).status,
            {"failed", "succeeded", "cancelled"},
            ENDED_WITHIN,
        )
        rows = stack.admin.sql("SELECT id FROM silver.x ORDER BY id", environment, timeout=60)
        with Session(stack.db) as session:
            attempts = session.execute(
                select(TaskAttempt.outcome, TaskAttempt.worker)
                .where(TaskAttempt.task_run_id == done.id)
                .order_by(TaskAttempt.started_at)
            ).all()
            expected = check_failure_message(
                verdict(results_of_task(session, done.id)).blocking,
                table="silver.x",
                run_id=done.run_id,
                environment=environment,
                rollback=Rollback(RollbackOutcome.ROLLED_BACK),
            )
        assert [tuple(a) for a in attempts] == [
            (AttemptOutcome.LOST, first.id),
            (AttemptOutcome.FAILED, second.id),
        ]
    assert (finished, done.status) == ("failed", TaskStatus.FAILED)
    assert done.error_message == expected
    assert expected.startswith(
        "Check `silver.x/b_small` failed: 1 row (checks/silver/x/b_small.sql). "
        + ROLLBACK_SENTENCES[RollbackOutcome.ROLLED_BACK]
    )
    runs = _task_query_runs(stack, done.id)
    writes = [r for r in runs if r.write_mode is not None]
    counts = [r for r in runs if r.write_mode is None]
    assert [(w.status, w.rows_written, w.worker_id) for w in writes] == [
        (QueryRunStatus.SUCCEEDED, 3, first.id)
    ]  # exactly one write run: the table was built once
    # The killed worker's count was ended by whoever came first: the sweep, with the text of
    # a count, or the reclaim of the task's lease, with the text of a lost task.
    assert (counts[0].status, counts[0].worker_id) == (QueryRunStatus.FAILED, first.id)
    assert counts[0].error_message in {CHECK_LOST_MESSAGE, WORKER_LOST_WRITE_MESSAGE}
    assert [(c.status, c.worker_id) for c in counts[1:]] == [
        (QueryRunStatus.SUCCEEDED, second.id),
        (QueryRunStatus.SUCCEEDED, second.id),
    ]
    results = stack.admin.checks.for_run(environment, run.id)
    assert [(r.check, r.outcome, r.failing_rows, r.rollback_outcome) for r in results] == [
        ("a_slow", "passed", 0, None),
        ("b_small", "failed", 1, "rolled_back"),
    ]
    below = task_row(stack.db, run.id, "gold.y")
    assert (below.status, below.attempt, below.ready_at) == (TaskStatus.UPSTREAM_FAILED, 0, None)
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id == before
    assert rows.column("id").to_pylist() == [7, 8]


@pytest.mark.parametrize("put_back_first", [False, True], ids=["cancel-first", "put-back-first"])
def test_a_cancelled_run_whose_worker_was_killed_after_its_write_is_set_back_by_the_next_worker(
    stack: QueryStack, tmp_path: Path, put_back_first: bool
) -> None:
    """A real worker process is killed while it counts its first check, and the run is cancelled.

    The table is written and the task stamped. Nobody checked it, and the cancel used to end the
    task, leaving the rows in the table. Now the task waits ready for a set-back claim: the
    worker that starts next sets the table back to before the run, builds nothing and counts
    nothing, and the task ends cancelled with the cancel message and what became of the table.
    That holds whether the cancel comes before the scheduler noticed the lost worker, or after
    it put the task back to wait for its checks.

    Parameters
    ----------
    stack
        The stack.
    tmp_path
        The workers' scratch root.
    put_back_first
        Cancel only once the scheduler has put the task back to wait for its checks.
    """
    environment, repository = stack.environment(), unique_name()
    stack.admin.tables.create(environment, "silver.x", schema=IDS)
    before = stack.admin.tables.append(
        environment, "silver.x", pa.table({"id": pa.array([7, 8], pa.int64())})
    ).snapshot_id
    path = stack.services.roots / repository
    slow = f"SELECT x.id FROM silver.x AS x, ({long_sql('n')}) AS q WHERE q.n IS NULL"
    write_check(path, "silver.x", "a_slow", slow)
    commit_files(
        path,
        {
            "lakana.yaml": "defaults:\n  retry_delay: 0\n",
            "assets/silver/x.sql": "SELECT id FROM bronze.nums\n",
            "assets/gold/y.sql": "SELECT count(*) AS n FROM silver.x\n",
        },
    )
    stack.admin.repositories.create(repository, "local", str(path))
    deploy = stack.admin.deploys.create(environment, repository)
    settings = DeploySettings(repo_roots=(str(stack.services.roots),))
    with Session(stack.db) as session:
        assert poll_once(session, GrantAuthorization(session), deploy=settings)
    stack.admin.deploys.wait(environment, deploy.id, timeout=30)

    def counting(task: TaskRun) -> bool:
        """Say whether the task is stamped and one of its counts is running.

        Parameters
        ----------
        task
            The task run.

        Returns
        -------
        bool
            Whether the kill would land between the write and the verdict.
        """
        return task.written_table_id is not None and any(
            run.write_mode is None and run.status is QueryRunStatus.RUNNING
            for run in _task_query_runs(stack, task.id)
        )

    rand = secrets.SystemRandom().random

    def tick() -> TaskRun:
        """Tick once, as the leader would, and read the task.

        Returns
        -------
        TaskRun
            The task run of ``silver.x``.
        """
        with Session(stack.db) as session:
            scheduler_tick(session, db_now(session), rand, lease_seconds=LEASE)
        return task_row(stack.db, run.id, "silver.x")

    def until_task(reached: Callable[[TaskRun], bool], seconds: float) -> TaskRun:
        """Tick every 0.2 s until the task is where the test wants it.

        Parameters
        ----------
        reached
            Says whether the wait is over.
        seconds
            The bound.

        Returns
        -------
        TaskRun
            The task run when `reached` said yes.
        """
        deadline = time.monotonic() + seconds
        while True:
            task = tick()
            assert task.status is not TaskStatus.SUCCEEDED, "the unchecked task counted as done"
            if reached(task):
                return task
            assert time.monotonic() < deadline, f"still {task.status} after {seconds:g} s"
            time.sleep(0.2)

    with _worker_process(stack, tmp_path / "first") as first:
        run = stack.admin.runs.start(environment, repository)
        until_task(counting, START_WITHIN + COUNT)
        first.kill()
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id != before  # unchecked rows
    if put_back_first:
        put_back = until_task(lambda task: task.lease_owner is None, ENDED_WITHIN)
        assert (put_back.status, put_back.setting_back) == (TaskStatus.READY, False)
    assert stack.admin.runs.cancel(environment, run.id).cancel_requested

    waiting = until_task(lambda task: task.setting_back, ENDED_WITHIN)
    assert (waiting.status, waiting.attempt) == (TaskStatus.READY, 1)
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id != before  # not yet

    with _worker_process(stack, tmp_path / "second") as second:
        done = until_task(lambda task: task.status is TaskStatus.CANCELLED, ENDED_WITHIN)
        rows = stack.admin.sql("SELECT id FROM silver.x ORDER BY id", environment, timeout=60)
        finished = until(
            lambda: (tick(), stack.admin.runs.get(environment, run.id).status)[1],
            {"failed", "succeeded", "cancelled"},
            ENDED_WITHIN,
        )
        assert done.lease_owner is None
        with Session(stack.db) as session:
            attempts = session.execute(
                select(TaskAttempt.outcome, TaskAttempt.worker)
                .where(TaskAttempt.task_run_id == done.id)
                .order_by(TaskAttempt.started_at)
            ).all()
    assert done.error_message == set_back_ending(RUN_CANCELLED_MESSAGE, SET_BACK_DONE)
    assert (finished, done.set_back_claims) == ("cancelled", 1)
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id == before
    assert rows.column("id").to_pylist() == [7, 8]
    assert [tuple(a) for a in attempts] == [
        (AttemptOutcome.LOST, first.id),
        (AttemptOutcome.CANCELLED, second.id),
    ]
    writes = [r for r in _task_query_runs(stack, done.id) if r.write_mode is not None]
    assert len(writes) == 1  # the table was built once, by the first worker only
