"""The scheduler's part for jobs: fire the due jobs and start the ones that wait.

Called by the scheduler leader on each tick. A fire is two transactions: `JobService.book_fire`
consumes the time (so it can never fire twice, whatever happens next), then `JobService.start`
starts the run or parks it behind the project's active run. A waiting start is started by
`JobService.start_waiting` once its project is free. Nothing here keeps a lock across the two,
because a deploy takes the deployment's pointer first and a lock held while starting would
deadlock with it.

One schedule's error is logged and does not reach the others. Two limits keep the leader's step
short, because the run engine's tick comes before this and must run again in a second: the time
of a tick is checked between two schedules (`TICK_BUDGET_SECONDS`), and on PostgreSQL every lock
wait in these transactions gives up after `LOCK_WAIT_SECONDS` (a deploy or a start by hand
holds the deployment's pointer only for moments, but a stuck connection could hold it much
longer). What a lock timeout interrupts is tried again on the next tick. The step can still take
the budget plus the schedule in progress, and each of its transactions waits at most
`LOCK_WAIT_SECONDS`.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import time
import uuid
from collections.abc import Callable, Collection, Generator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, event, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.orm.session import SessionTransaction

from lakana_core.audit.service import AuditService
from lakana_core.auth import Securable
from lakana_core.errors import ConflictError, NotFoundError, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.jobs.audit import start_detail
from lakana_core.jobs.service import (
    PROBLEM_NONE,
    PROBLEM_UNUSABLE,
    BookedFire,
    JobService,
    run_principal_problem,
)
from lakana_core.models import (
    AuditEventType,
    JobFire,
    JobSchedule,
    Run,
    RunTrigger,
)
from lakana_core.models.run import ACTIVE_RUN_STATUSES
from lakana_core.projects.deploys import DeploymentMoved
from lakana_core.projects.run_policy import RunCaps
from lakana_core.runs.leader import STALE_AFTER
from lakana_core.runs.pools import is_lock_timeout

logger = logging.getLogger(__name__)

TICK_BUDGET_SECONDS = STALE_AFTER.total_seconds() / 3
"""How long the job steps of one scheduler tick may take. The leader renews its role once per
step, and another worker takes it over after `STALE_AFTER`, so one slow start must not use up
the time; what is left over is done on the next tick."""


LOG_EVERY_SECONDS = 60.0
"""Least time between two log lines about the same failing schedule."""

LOCK_WAIT_SECONDS = 2.0
"""Longest a transaction of the job steps waits for a lock on PostgreSQL."""

_LOGGED_KEEP = 256
"""How many schedules the log limit remembers before it forgets the ones past their window."""

_RECENTLY_STUCK: dict[tuple[uuid.UUID, uuid.UUID], float] = {}
"""When each project last made a lock wait time out; such a project goes last for a while."""

_LAST_LOGGED: dict[tuple[str, uuid.UUID], float] = {}


def _may_log(kind: str, key: uuid.UUID, moment: float) -> bool:
    """Say whether a failure of a schedule may be logged now, and remember that it was.

    A schedule that fails on every tick would otherwise write a traceback each second.

    Parameters
    ----------
    kind
        What failed (booking, starting, a lock wait).
    key
        The schedule's id.
    moment
        The monotonic time now.

    Returns
    -------
    bool
        Whether `LOG_EVERY_SECONDS` have passed since the last line of this kind for this key.
    """
    last = _LAST_LOGGED.get((kind, key))
    if last is not None and moment - last < LOG_EVERY_SECONDS:
        return False
    if len(_LAST_LOGGED) >= _LOGGED_KEEP:
        for stale in [k for k, seen in _LAST_LOGGED.items() if moment - seen >= LOG_EVERY_SECONDS]:
            del _LAST_LOGGED[stale]
    _LAST_LOGGED[kind, key] = moment
    return True


@contextlib.contextmanager
def bounded_lock_waits(session: Session, seconds: float) -> Generator[None]:
    """Make every transaction a session begins give up on a lock after some seconds.

    ``SET LOCAL`` ends with the transaction, so it is issued each time one begins (a commit or a
    rollback ends it) and never reaches another user of the pooled connection. Other databases
    have no such wait to bound (SQLite has one writer and a busy timeout of its own), so
    nothing is installed there.

    Parameters
    ----------
    session
        The session the job steps use; it should hold no transaction yet.
    seconds
        The longest wait for a lock.

    Yields
    ------
    None
        While the limit is installed.
    """
    if session.get_bind().dialect.name != "postgresql":
        yield
        return
    milliseconds = int(seconds * 1000)

    def limit(_session: Session, _transaction: SessionTransaction, connection: Connection) -> None:
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{milliseconds}ms'")

    event.listen(session, "after_begin", limit)
    try:
        yield
    finally:
        event.remove(session, "after_begin", limit)


class FireOutcome(enum.Enum):
    """What became of a scheduled time that was consumed."""

    STARTED = "started"
    WAITING = "waiting"
    NO_RUN_PRINCIPAL = "no_run_principal"
    NOT_STARTED = "not_started"


@dataclass(frozen=True)
class Fire:
    """One scheduled time that was fired.

    Parameters
    ----------
    job
        The job.
    environment
        Environment name.
    repository
        Repository name.
    scheduled_for
        The scheduled time.
    missed
        How many older scheduled times it stands in for.
    outcome
        What became of it.
    run_id
        The run it started, if it did.
    """

    job: str
    environment: str
    repository: str
    scheduled_for: datetime
    missed: int
    outcome: FireOutcome
    run_id: uuid.UUID | None = None


def _expired(monotonic: Callable[[], float], deadline: float) -> bool:
    """Say whether a tick's time is used up.

    Parameters
    ----------
    monotonic
        The clock the deadline is read from.
    deadline
        When the time is up.

    Returns
    -------
    bool
        Whether `monotonic()` has reached the deadline.
    """
    return monotonic() >= deadline


def fire_due(
    session: Session,
    now: datetime,
    *,
    caps: RunCaps | None = None,
    lease_seconds: int = 60,
    time_limit: float = TICK_BUDGET_SECONDS,
    lock_timeout: float = LOCK_WAIT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    stuck: set[tuple[uuid.UUID, uuid.UUID]] | None = None,
) -> list[Fire]:
    """Fire every schedule that is due, each in its own transactions.

    A schedule that is not finished when the time limit is spent stays due and is fired on the
    next tick, and so does one whose booking waited too long for a lock; after such a wait the
    project's other schedules are left for the next tick too, so that one stuck deployment
    cannot use the time of the others. An error in one
    schedule is logged (a traceback at most once a minute) and the others go on. A time that was
    booked but whose start failed is consumed; `JOB_START_DROPPED` records why (unless no run
    principal is bound, which `lakana job list` shows).

    Parameters
    ----------
    session
        Database session; commits and rolls back by itself.
    now
        The database's current time (the scheduler's, not this host's).
    caps
        Operator maximums applied to each task's policy.
    lease_seconds
        As for `JobService`.
    time_limit
        Seconds this call may take at most, checked between two schedules.
    lock_timeout
        Seconds a transaction waits for a lock on PostgreSQL before it gives up.
    monotonic
        The clock the limit is measured on.
    stuck
        A set to fill with the (environment, repository) pairs whose lock wait timed out in this
        call, for `start_waiting_jobs` to skip in the same tick.

    Returns
    -------
    list of Fire
        The times that were consumed in this call, in the order of the schedules' due times.
    """
    deadline = monotonic() + time_limit
    session.rollback()
    with bounded_lock_waits(session, lock_timeout):
        due = session.execute(
            select(
                JobSchedule.id,
                JobSchedule.job,
                JobSchedule.environment_id,
                JobSchedule.repository_id,
            )
            .where(
                JobSchedule.next_fire_at.is_not(None),
                JobSchedule.next_fire_at <= now,
                JobSchedule.paused.is_(False),
            )
            .order_by(JobSchedule.next_fire_at, JobSchedule.id)
        ).all()
        session.rollback()
        fires: list[Fire] = []
        stuck = set() if stuck is None else stuck
        for schedule_id, job, env_id, repo_id in due:
            if _expired(monotonic, deadline):
                break
            if (env_id, repo_id) in stuck:
                continue
            try:
                booked = JobService.book_fire(session, schedule_id, now)
            except Exception as exc:
                session.rollback()
                _report("booking", schedule_id, job, exc, monotonic())
                if _is_stuck(exc):
                    stuck.add((env_id, repo_id))
                    _RECENTLY_STUCK[env_id, repo_id] = monotonic()
                continue
            if booked is not None:
                fire, waited = _start(session, booked, caps, lease_seconds, monotonic)
                fires.append(fire)
                if waited:
                    stuck.add((env_id, repo_id))
                    _RECENTLY_STUCK[env_id, repo_id] = monotonic()
    return fires


def _lately_stuck(project: tuple[uuid.UUID, uuid.UUID], moment: float) -> bool:
    """Say whether a project's lock wait timed out within the last minute.

    Parameters
    ----------
    project
        The (environment, repository) pair.
    moment
        The monotonic time now.

    Returns
    -------
    bool
        `True` if so; such a project is served after the others.
    """
    seen = _RECENTLY_STUCK.get(project)
    return seen is not None and moment - seen < LOG_EVERY_SECONDS


def _is_stuck(error: Exception) -> bool:
    """Say whether an error means the project's deployment is held by someone else.

    Parameters
    ----------
    error
        What a step of a schedule raised.

    Returns
    -------
    bool
        `True` for a lock timeout, or a pointer that kept moving: the project's other
        schedules would only wait as long, so the tick leaves the project to the next one.
    """
    return isinstance(error, DeploymentMoved) or (
        isinstance(error, DBAPIError) and is_lock_timeout(error)
    )


def _report(kind: str, schedule_id: uuid.UUID, job: str, error: Exception, moment: float) -> None:
    """Log a failure of a schedule's step, rarely.

    Parameters
    ----------
    kind
        What failed.
    schedule_id
        The schedule's id.
    job
        The job's name.
    error
        What was raised.
    moment
        The monotonic time now.
    """
    if not _may_log(kind, schedule_id, moment):
        return
    if _is_stuck(error):
        logger.warning(
            "The %s of job %r waited too long for a lock on the project (a deploy or a start is "
            "holding its deployment, or the deployment kept moving); it is tried again on the "
            "next tick.",
            kind,
            job,
        )
    else:
        logger.error("The %s of job %r failed; continuing.", kind, job, exc_info=error)


def _start(
    session: Session,
    booked: BookedFire,
    caps: RunCaps | None,
    lease_seconds: int,
    monotonic: Callable[[], float],
) -> tuple[Fire, bool]:
    """Start a booked fire, or park it behind the active run.

    Parameters
    ----------
    session
        Database session.
    booked
        The fire.
    caps
        Operator maximums applied to each task's policy.
    lease_seconds
        As for `JobService`.
    monotonic
        The clock that paces the log.

    Returns
    -------
    tuple of (Fire, bool)
        What became of it (the time stays consumed in every case), and whether the start
        waited too long for a lock.
    """
    outcome = FireOutcome.NOT_STARTED
    run_id: uuid.UUID | None = None
    reason: str | None = None
    failure: Exception | None = None
    try:
        started = JobService(
            session, GrantAuthorization(session), caps=caps, lease_seconds=lease_seconds
        ).start(
            None,
            booked.environment,
            booked.repository,
            booked.job,
            trigger=RunTrigger.SCHEDULE,
            scheduled_for=booked.scheduled_for,
            missed=booked.missed,
        )
    except ConflictError as exc:
        session.rollback()
        failure = exc
        try:
            problem = run_principal_problem(session, booked.environment_id, booked.repository_id)
        except Exception:
            problem = None
        session.rollback()
        if problem == PROBLEM_NONE:
            outcome = FireOutcome.NO_RUN_PRINCIPAL
        else:
            reason = "run_principal_unusable" if problem == PROBLEM_UNUSABLE else "error"
    except ValidationError as exc:
        session.rollback()
        failure, reason = exc, "selection_invalid"
    except NotFoundError as exc:
        session.rollback()
        failure, reason = exc, "job_removed"
    except Exception as exc:
        session.rollback()
        failure, reason = exc, "error"
    else:
        if started.run is not None:
            outcome, run_id = FireOutcome.STARTED, started.run.id
            try:
                session.execute(
                    update(JobFire).where(JobFire.id == booked.fire_id).values(run_id=run_id)
                )
                session.commit()
            except Exception as exc:
                session.rollback()
                _report("link to its run", booked.schedule_id, booked.job, exc, monotonic())
        else:
            outcome = FireOutcome.WAITING
    if failure is not None and _may_log("start", booked.schedule_id, monotonic()):
        if reason == "error" and not (isinstance(failure, DBAPIError) and is_lock_timeout(failure)):
            logger.error(
                "Starting job %r for %s failed; that time is consumed. Continuing.",
                booked.job,
                booked.scheduled_for.isoformat(),
                exc_info=failure,
            )
        else:
            logger.warning(
                "Job %r in %r did not start for %s: %s",
                booked.job,
                booked.environment,
                booked.scheduled_for.isoformat(),
                failure,
            )
    if reason is not None:
        _audit_unstarted(session, booked, reason)
    fire = Fire(
        job=booked.job,
        environment=booked.environment,
        repository=booked.repository,
        scheduled_for=booked.scheduled_for,
        missed=booked.missed,
        outcome=outcome,
        run_id=run_id,
    )
    return fire, failure is not None and _is_stuck(failure)


def _audit_unstarted(session: Session, booked: BookedFire, reason: str) -> None:
    """Record, in a transaction of its own, that a consumed time did not start its job.

    The system did this, so no principal is the actor. Without the event the only trace would
    be the worker's log, which the people who own the job do not read.

    Parameters
    ----------
    session
        Database session.
    booked
        The fire.
    reason
        ``run_principal_unusable``, ``selection_invalid``, ``job_removed`` or ``error``.
    """
    try:
        AuditService(session).record(
            None,
            AuditEventType.JOB_START_DROPPED,
            securable=Securable.environment_scope(booked.environment),
            detail=start_detail(
                booked.repository,
                booked.job,
                {
                    "trigger": RunTrigger.SCHEDULE.value,
                    "scheduled_for": booked.scheduled_for.isoformat(),
                    "missed": booked.missed,
                },
                reason=reason,
            ),
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Recording the dropped start of job %r failed.", booked.job)


def _requested(schedule: JobSchedule) -> datetime:
    """Read when a waiting start was first requested, to order them.

    Parameters
    ----------
    schedule
        A schedule with a waiting start.

    Returns
    -------
    datetime
        The time (UTC when the text names no offset); the latest possible time when it cannot be
        read, so that a start that `start_waiting` is about to drop does not go first.
    """
    pending = schedule.pending_start or {}
    try:
        moment = datetime.fromisoformat(str(pending["requested_at"]))
    except (KeyError, ValueError):
        return datetime.max.replace(tzinfo=UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def start_waiting_jobs(
    session: Session,
    *,
    caps: RunCaps | None = None,
    lease_seconds: int = 60,
    time_limit: float = TICK_BUDGET_SECONDS,
    lock_timeout: float = LOCK_WAIT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    skip: Collection[tuple[uuid.UUID, uuid.UUID]] = (),
) -> list[Run]:
    """Start the oldest waiting start of every project that has no active run.

    Only one run can be active in a project, so after one starts the others keep waiting. A
    start that is dropped (its check failed) or stuck (`ConflictError` or a lock timeout, tried
    again next time) does not hold up the next one of its project.

    Parameters
    ----------
    session
        Database session; commits and rolls back by itself.
    caps
        Operator maximums applied to each task's policy.
    lease_seconds
        As for `JobService`.
    time_limit
        Seconds this call may take at most, checked between two starts.
    lock_timeout
        Seconds a transaction waits for a lock on PostgreSQL before it gives up.
    monotonic
        The clock the limit is measured on.
    skip
        (Environment, repository) pairs to leave alone in this call, the ones `fire_due` found
        stuck. Projects that timed out lately and are not in it go last.

    Returns
    -------
    list of Run
        The runs that were started.
    """
    deadline = monotonic() + time_limit
    session.rollback()
    started: list[Run] = []
    with bounded_lock_waits(session, lock_timeout):
        waiting = list(
            session.scalars(select(JobSchedule).where(JobSchedule.pending_start.is_not(None)))
        )
        by_project: dict[tuple[uuid.UUID, uuid.UUID], list[JobSchedule]] = {}
        for schedule in sorted(waiting, key=_requested):
            by_project.setdefault((schedule.environment_id, schedule.repository_id), []).append(
                schedule
            )
        projects = [
            (key, [(s.id, s.job) for s in rows])
            for key, rows in by_project.items()
            if key not in skip
        ]
        projects.sort(key=lambda p: _lately_stuck(p[0], monotonic()))
        session.rollback()
        for (env_id, repo_id), candidates in projects:
            if _active(session, env_id, repo_id):
                continue
            for schedule_id, job in candidates:
                if _expired(monotonic, deadline):
                    return started
                try:
                    row = session.get(JobSchedule, schedule_id)
                    if row is None:
                        continue
                    run = JobService.start_waiting(
                        session, row, caps=caps, lease_seconds=lease_seconds
                    )
                except Exception as exc:
                    session.rollback()
                    if isinstance(exc, ConflictError):
                        logger.info("The waiting start of job %r will be tried again: %s", job, exc)
                    else:
                        _report("waiting start", schedule_id, job, exc, monotonic())
                    if _is_stuck(exc):
                        _RECENTLY_STUCK[env_id, repo_id] = monotonic()
                        break
                    continue
                if run is not None:
                    started.append(run)
                if _active(session, env_id, repo_id):
                    break
    return started


def _active(session: Session, env_id: uuid.UUID, repo_id: uuid.UUID) -> bool:
    """Say whether a project has an active run.

    Parameters
    ----------
    session
        Database session.
    env_id
        The environment.
    repo_id
        The repository.

    Returns
    -------
    bool
        Whether a run is queued or running.
    """
    session.rollback()
    return (
        session.execute(
            select(Run.id)
            .where(
                Run.environment_id == env_id,
                Run.repository_id == repo_id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
            )
            .limit(1)
        ).first()
        is not None
    )


def sweep_job_fires(session: Session) -> int:
    """Delete the fires older than the last fire of their schedule.

    The latest one stays: it is what a second leader racing over the same time collides with.

    Parameters
    ----------
    session
        Database session; commits.

    Returns
    -------
    int
        How many rows were deleted.
    """
    latest = (
        select(JobSchedule.last_fired_for)
        .where(JobSchedule.id == JobFire.job_schedule_id)
        .scalar_subquery()
    )
    deleted = session.execute(
        delete(JobFire).where(JobFire.scheduled_for < latest).returning(JobFire.id)
    ).all()
    session.commit()
    return len(deleted)
