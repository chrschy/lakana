"""The scheduler role inside a worker: elect, tick, repeat.

Runs in its own daemon thread so a long task in the worker's main loop never
stalls scheduling. Each step opens its own session; nothing a step raises
stops the thread.

The sweep of lost runs has a guard against the leader's own absence. A worker's next
heartbeat can be `renew_seconds` away at the moment the leader's next tick is one second away.
After a pause of the whole stack, a stall of the database or a leader that was away (suspended,
backing off, newly elected), heartbeats are as old as the pause and the leader's first tick
would end the queries of every healthy worker before any of them could beat. So the loop
remembers the database time of its previous tick; when the gap to this one, less the loop's own
interval, is more than half the lease, or there was none (a new leader), it sweeps neither
query runs nor deploy runs until `renew_seconds + RETRY_SECONDS` of database time have passed
since that late tick. The interval is subtracted because it is the gap of a healthy loop: with
an interval of half the lease or more every tick would otherwise count as late, re-arm the
pause before it ended, and the sweep would never run. A pause cannot outlast the ticks for ever:
it ends `renew_seconds + RETRY_SECONDS` after the late tick, and a tick re-arms it only after a
stall of more than half the lease beyond its interval; with an interval above that bound the next
tick comes after the pause anyway. A leader that lost the role and took it back is late only if
its previous leader tick is that far back (it then did not look at the database that long).
Task-lease reclaim, which does not depend on heartbeats, runs on every tick as before.
The gap is measured on the database's time (`_now`), never on this host's clock, which a
suspend also stops.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.jobs.scheduler import TICK_BUDGET_SECONDS, fire_due, start_waiting_jobs
from lakana_core.log_events import LogEvent, log_event
from lakana_core.projects.run_policy import RunCaps
from lakana_core.runs.leader import LeaderElection
from lakana_core.runs.store import scheduler_tick
from lakana_worker.heartbeat import RETRY_SECONDS

logger = logging.getLogger(__name__)

_MAX_EXPONENT = 1000
"""Keeps `2.0**exponent` a finite float; the wait is clamped after multiplying, so the
cap is reached for any positive interval (16 used to stop tiny intervals short of it)."""

HOUSEKEEPING_JOIN_SECONDS = 5.0
"""How long shutdown waits for a running housekeeping thread before it resigns anyway."""

FORGET_INTERVAL_SECONDS = 60.0
"""Least database time between two deletions of the rows of workers silent for an hour."""

MAX_BACKOFF_SECONDS = 30.0
"""Longest wait between steps while the database keeps failing."""


class SchedulerLoop:
    """Hold (or wait for) the scheduler role and tick while holding it.

    Parameters
    ----------
    engine
        The metadata database engine.
    holder
        This worker's id.
    interval
        Seconds between steps (`LAKANA_RUN__SCHEDULER_INTERVAL_SECONDS`).
    clock
        Current time. `None` (production) reads the database's clock each
        step, so lease and heartbeat times never depend on this host's wall
        clock; tests inject a fixed one.
    rand
        Jitter source; `secrets.SystemRandom().random` when `None`.
    election
        Leader election; `LeaderElection(engine, holder)` when `None`.
    housekeeping
        Called with the step's time and the loop's stop event after a tick, while this worker
        leads, at most once per `housekeeping_interval` seconds (the artifact sweep). It runs
        in a daemon thread of its own and never more than one at a time: the step starts it
        and does not wait, so a hung object-store call or a wait for a lock stalls only the
        housekeeping, never lease reclaim or task promotion. It should end soon after the stop
        event is set. An exception from it is logged and does not make the step fail or back
        the scheduler off.
    housekeeping_interval
        Seconds between starts of `housekeeping`, counted from the start of the last one
        (also when it failed, so a failing one is not retried on every step); 0 never runs it
        (`LAKANA_RUN__SWEEP_INTERVAL`).
    monotonic
        Clock that paces `housekeeping`; `time.monotonic` when `None`.
    housekeeping_limit
        Seconds one `housekeeping` run is meant to take at most (the sweep's deadline). A run
        that is still going after twice that, when the next one is due, is reported in the log
        once; nothing else starts until it ends.
    lease_seconds
        `LAKANA_RUN__LEASE_SECONDS`, handed to the leader's ticks: they then also end the
        `RUNNING` query and deploy runs no alive worker holds, except right after a gap
        (see the module docstring). `None` sweeps nothing; `main` always passes the setting.
    renew_seconds
        `LAKANA_RUN__RENEW_SECONDS`: after a late tick the sweep waits this long plus
        `RETRY_SECONDS` of database time, so that every healthy worker can beat once.
    caps
        Operator maximums for the runs that scheduled jobs start; none when `None`.
    """

    def __init__(
        self,
        engine: Engine,
        holder: str,
        *,
        interval: float = 1.0,
        clock: Callable[[], datetime] | None = None,
        rand: Callable[[], float] | None = None,
        election: LeaderElection | None = None,
        housekeeping: Callable[[datetime, threading.Event], None] | None = None,
        housekeeping_interval: float = 3600.0,
        monotonic: Callable[[], float] | None = None,
        housekeeping_limit: float = 300.0,
        lease_seconds: int | None = None,
        renew_seconds: float = 20.0,
        caps: RunCaps | None = None,
    ) -> None:
        self._caps = caps
        self._lease_seconds = lease_seconds
        self._renew_seconds = renew_seconds
        self._previous_tick: datetime | None = None
        self._quiet_until: datetime | None = None
        self._last_forget: datetime | None = None
        self._engine = engine
        self._interval = interval
        self._clock = clock
        self._rand = rand or secrets.SystemRandom().random
        self._election = election or LeaderElection(engine, holder)
        self._stop = threading.Event()
        self._failures = 0
        self._housekeeping = housekeeping
        self._housekeeping_interval = housekeeping_interval
        self._monotonic = monotonic or time.monotonic
        self._last_housekeeping: float | None = None
        self._housekeeping_thread: threading.Thread | None = None
        self._housekeeping_limit = housekeeping_limit
        self._hang_reported = False
        self._leading = False

    def _note_role(self, leading: bool) -> None:
        """Log when this worker takes the scheduler role and when it no longer holds it.

        Parameters
        ----------
        leading
            Whether this worker holds the role after this step's election.
        """
        if leading == self._leading:
            return
        self._leading = leading
        if leading:
            logger.info(
                "This worker holds the scheduler role now.",
                extra=log_event(LogEvent.SCHEDULER_ROLE_TAKEN),
            )
        else:
            logger.warning(
                "This worker no longer holds the scheduler role (its database connection failed, "
                "or another worker took it over).",
                extra=log_event(LogEvent.SCHEDULER_ROLE_LOST),
            )

    def _now(self) -> datetime:
        """Return the step's time: the injected clock, else the database's.

        Returns
        -------
        datetime
            An aware UTC timestamp.
        """
        if self._clock is not None:
            return self._clock()
        with Session(self._engine) as session:
            return db_now(session)

    def _sweep_plan(self, now: datetime) -> tuple[int | None, bool]:
        """Decide what this tick sweeps: the lease to hand over (or `None`) and whether to forget.

        Parameters
        ----------
        now
            The step's database time.

        Returns
        -------
        tuple
            The lease for `scheduler_tick` (`None`: no sweep, as for a late tick), and whether
            this tick deletes the rows of workers silent for an hour (at most once a minute).
        """
        lease = self._lease_seconds
        if lease is None:
            return None, False
        previous, self._previous_tick = self._previous_tick, now
        if previous is None or (now - previous).total_seconds() - self._interval > lease / 2:
            self._quiet_until = now + timedelta(seconds=self._renew_seconds + RETRY_SECONDS)
        if self._quiet_until is not None and now < self._quiet_until:
            return None, False
        due = (
            self._last_forget is None
            or (now - self._last_forget).total_seconds() >= FORGET_INTERVAL_SECONDS
        )
        if due:
            self._last_forget = now
        return lease, due

    def step(self) -> bool:
        """Try to lead and, if leading, tick once.

        Returns
        -------
        bool
            Whether this worker held the role and ticked.
        """
        try:
            now = self._now()
            leading = self._election.try_lead(now)
            self._note_role(leading)
            if leading:
                lease, forget = self._sweep_plan(now)
                with Session(self._engine) as session:
                    scheduler_tick(session, now, self._rand, lease, forget=forget)
                self._run_jobs(now)
                self._run_housekeeping(now)
        except Exception as error:
            self._failures += 1
            if self._failures == 1:
                logger.exception(
                    "Scheduler step failed; continuing.",
                    extra=log_event(LogEvent.SCHEDULER_STEP_FAILED),
                )
            else:
                logger.warning(
                    "Scheduler step still failing (%d in a row): %s",
                    self._failures,
                    error,
                    extra=log_event(LogEvent.SCHEDULER_STEP_FAILED),
                )
            return False
        self._failures = 0
        return leading

    def _run_jobs(self, now: datetime) -> None:
        """Fire the due jobs, then start the waiting ones, within one time budget.

        The leader renews its role once per step and loses it after `STALE_AFTER`, so the two
        job steps share `TICK_BUDGET_SECONDS`; what they leave undone is done on the next step.
        A failure is logged and does not fail the step, because the run engine's tick before it
        has already been applied.

        Parameters
        ----------
        now
            The step's time.
        """
        started = self._monotonic()
        stuck: set[tuple[uuid.UUID, uuid.UUID]] = set()
        lease = self._lease_seconds if self._lease_seconds is not None else 60
        try:
            with Session(self._engine) as session:
                fire_due(
                    session,
                    now,
                    caps=self._caps,
                    lease_seconds=lease,
                    time_limit=TICK_BUDGET_SECONDS,
                    stuck=stuck,
                )
        except Exception:
            logger.exception("Firing scheduled jobs failed; continuing.")
        left = max(TICK_BUDGET_SECONDS - (self._monotonic() - started), 0.0)
        try:
            with Session(self._engine) as session:
                start_waiting_jobs(
                    session, caps=self._caps, lease_seconds=lease, time_limit=left, skip=stuck
                )
        except Exception:
            logger.exception("Starting waiting jobs failed; continuing.")

    def _run_housekeeping(self, now: datetime) -> None:
        """Start the housekeeping in its own thread if it is due and none is running.

        Parameters
        ----------
        now
            The step's time.
        """
        if self._housekeeping is None or self._housekeeping_interval <= 0:
            return
        running = self._housekeeping_thread
        moment = self._monotonic()
        due = (
            self._last_housekeeping is None
            or moment - self._last_housekeeping >= self._housekeeping_interval
        )
        if running is not None and running.is_alive():
            started = moment if self._last_housekeeping is None else self._last_housekeeping
            elapsed = moment - started
            if due and elapsed > 2 * self._housekeeping_limit and not self._hang_reported:
                self._hang_reported = True
                logger.warning(
                    "Housekeeping has been running for %d s, more than twice its limit of %d s, "
                    "so no further run starts until it ends; it may be waiting on storage or "
                    "the database.",
                    elapsed,
                    self._housekeeping_limit,
                    extra=log_event(LogEvent.SWEEP_HUNG),
                )
            return
        if not due:
            return
        self._hang_reported = False
        self._last_housekeeping = moment
        self._housekeeping_thread = threading.Thread(
            target=self._housekeep, args=(now,), name="lakana-housekeeping", daemon=True
        )
        self._housekeeping_thread.start()

    def _housekeep(self, now: datetime) -> None:
        """Run the housekeeping; a failure is logged and goes no further.

        Parameters
        ----------
        now
            The step's time.
        """
        assert self._housekeeping is not None  # nosec B101 -- only started when it is set
        try:
            self._housekeeping(now, self._stop)
        except Exception:
            logger.exception(
                "Housekeeping failed; continuing.", extra=log_event(LogEvent.SWEEP_FAILED)
            )

    def join_housekeeping(self, timeout: float) -> bool:
        """Wait for a running housekeeping thread.

        Parameters
        ----------
        timeout
            Seconds to wait at most.

        Returns
        -------
        bool
            `True` if none is running (any more).
        """
        thread = self._housekeeping_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def next_wait(self) -> float:
        """Seconds to wait before the next step.

        Returns
        -------
        float
            `interval` normally; doubled per consecutive failed step, capped at
            `MAX_BACKOFF_SECONDS` (or `interval` if that is larger).
        """
        if self._failures == 0:
            return self._interval
        cap = max(MAX_BACKOFF_SECONDS, self._interval)
        exponent = min(self._failures, _MAX_EXPONENT)
        return min(self._interval * 2.0**exponent, cap)

    def run(self) -> None:
        """Step every `next_wait` seconds until `stop`, then resign."""
        try:
            while not self._stop.is_set():
                self.step()
                self._stop.wait(self.next_wait)
        finally:
            if not self.join_housekeeping(HOUSEKEEPING_JOIN_SECONDS):
                logger.warning("The housekeeping thread did not stop within the shutdown wait.")
            try:
                self._election.resign()
            except Exception:
                logger.exception("Resigning the scheduler role failed.")

    def start(self) -> threading.Thread:
        """Run in a daemon thread.

        Returns
        -------
        threading.Thread
            The started thread.
        """
        thread = threading.Thread(target=self.run, name="lakana-scheduler", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Ask the loop to end after its current step."""
        self._stop.set()
