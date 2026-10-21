"""The lease of a running task: its keeper, its loss and the check for a stop.

Holds `LeaseKeeper` (renews the lease in the background and decides when to interrupt the
task), `LeaseLost` (the claim is no longer this worker's), `cancel_kind` (whether a failure is
really a cancel or a release) and `keeper_for` (a keeper for a claim). The build, the check
phase and the Python runtime all run under one, so they share this module rather than each
importing the run engine.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.log_events import LogEvent, log_event
from lakana_core.models import Run
from lakana_core.runs.checks import run_stop_message
from lakana_core.runs.tasks import (
    ClaimedTask,
    FailureKind,
    LeaseState,
    renew_lease,
)

if TYPE_CHECKING:
    from lakana_worker.tasks import RunEngine

logger = logging.getLogger("lakana_worker.tasks")
"""The name `lakana_worker.tasks` logged under before the split, kept for log filters."""


_POLL_SECONDS = 1.0
"""How often a lease keeper wakes to check drain and renewal time."""


class LeaseKeeper:
    """Renew a task's lease in the background and decide when to interrupt it.

    Parameters
    ----------
    session_factory
        Opens a fresh session per renewal.
    task_run_id
        The running task run.
    worker
        This worker's id.
    claim
        `ClaimedTask.claim`; renewals are conditional on it, so a zombie of
        an earlier claim never extends the current lease.
    lease_seconds
        Lease length.
    renew_seconds
        Seconds between renewals.
    drain
        The worker's drain event.
    drain_grace_seconds
        Grace after the drain before the task is released.
    setting_back
        Whether the claim only sets the task's table back (`ClaimedTask.setting_back`); a cancel
        request is then no reason to stop, because that is the cancel's own work.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        task_run_id: uuid.UUID,
        worker: str,
        claim: int,
        *,
        lease_seconds: int,
        renew_seconds: float,
        drain: threading.Event | None = None,
        drain_grace_seconds: float = 0.0,
        setting_back: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._task_run_id = task_run_id
        self._worker = worker
        self._claim = claim
        self._lease_seconds = lease_seconds
        self._renew_seconds = renew_seconds
        self._drain = drain
        self._grace = drain_grace_seconds
        self.setting_back = setting_back
        self._next_renewal = time.monotonic() + renew_seconds
        self._drain_since: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cancel = threading.Event()
        self.reason: FailureKind | None = None
        self.lost = False

    def _signal(self, reason: FailureKind) -> None:
        """Interrupt the task for `reason` (the first reason wins).

        Parameters
        ----------
        reason
            ``CANCELLED`` or ``RELEASED``.
        """
        if self.reason is None:
            self.reason = reason
        self.cancel.set()

    def renew_once(self) -> None:
        """Check the drain grace, and renew the lease when it is due."""
        if self._drain is not None and self._drain.is_set():
            if self._drain_since is None:
                self._drain_since = time.monotonic()
            if time.monotonic() - self._drain_since >= self._grace:
                self._signal(FailureKind.RELEASED)
        if time.monotonic() < self._next_renewal:
            return
        self._next_renewal = time.monotonic() + self._renew_seconds
        try:
            with self._session_factory() as session:
                state = renew_lease(
                    session, self._task_run_id, self._worker, self._claim, self._lease_seconds
                )
        except Exception:
            logger.exception(
                "Lease renewal of task run %s failed; retrying.",
                self._task_run_id,
                extra=log_event(LogEvent.LEASE_RENEWAL_FAILED),
            )
            return
        if state is LeaseState.LOST:
            self.lost = True
            self.cancel.set()
        elif state is LeaseState.CANCEL_REQUESTED:
            self._signal(FailureKind.CANCELLED)

    def _run(self) -> None:
        """Wake every `_POLL_SECONDS` until stopped."""
        while not self._stop.wait(_POLL_SECONDS):
            self.renew_once()

    def start(self) -> None:
        """Start the background thread."""
        self._thread = threading.Thread(target=self._run, name="lakana-lease", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread and wait for it; safe to call twice."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None


class LeaseLost(Exception):
    """The claim is no longer this worker's: a zombie attempt that must do nothing."""


def cancel_kind(
    session: Session, keeper: LeaseKeeper, run_id: uuid.UUID
) -> tuple[FailureKind, str | None] | None:
    """Find out whether a failure is really a cancellation or a release.

    The keeper only learns of a cancel request at a renewal, but the
    protected-write guard refuses the moment `cancel_requested` is set, so a
    refusal can arrive before the keeper has seen anything. The flag is
    therefore re-read, except for a claim that only sets the table back, which a cancel request
    does not stop.

    Parameters
    ----------
    session
        Database session.
    keeper
        The (stopped) lease keeper.
    run_id
        The run.

    Returns
    -------
    tuple or None
        The kind and the message to record (`None` to keep the stored one), or
        `None` when neither the keeper nor the run asked to stop.
    """
    if keeper.reason is FailureKind.RELEASED:
        return keeper.reason, None
    session.rollback()
    if keeper.reason is None and keeper.setting_back:
        # Setting the table back is the cancel's own work, so a request is no reason to stop it.
        return None
    found = session.execute(
        select(Run.cancel_requested, Run.error_message).where(Run.id == run_id)
    ).one_or_none()
    session.rollback()
    if keeper.reason is None and (found is None or not found[0]):
        return None
    return FailureKind.CANCELLED, run_stop_message(None if found is None else found[1])


def keeper_for(engine: RunEngine, claimed: ClaimedTask) -> LeaseKeeper:
    """Build a lease keeper for a claim; the build and the check phase each run under one.

    Parameters
    ----------
    engine
        The run engine.
    claimed
        The claim.

    Returns
    -------
    LeaseKeeper
        A keeper that is not started.
    """
    settings = engine.settings
    return LeaseKeeper(
        engine.session_factory,
        claimed.task_run_id,
        settings.worker_id,
        claimed.claim,
        lease_seconds=settings.lease_seconds,
        renew_seconds=settings.renew_seconds,
        drain=engine.drain,
        drain_grace_seconds=settings.drain_grace_seconds,
        setting_back=claimed.setting_back,
    )
