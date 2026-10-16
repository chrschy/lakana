"""The lease keeper of a running task: renewal, drain and loss."""

from __future__ import annotations

import threading
import time
import types
import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlglot.errors import ParseError, TokenError
from task_run_helpers import World, _claim, _start, _task

import lakana_worker.task_lease as task_lease_module
from lakana_core.errors import (
    ConflictError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.models import (
    Run,
)
from lakana_core.runs.lockdown import WriteFenceRefused
from lakana_core.runs.tasks import (
    FailureKind,
    LeaseState,
)
from lakana_worker.executor import (
    OutOfMemoryQuery,
    QueryCancelled,
    QueryTimeout,
    TransientQueryError,
)
from lakana_worker.task_lease import LeaseKeeper
from lakana_worker.task_outcome import (
    classify,
)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (None, FailureKind.PERMANENT),
        (QueryCancelled("x"), FailureKind.CANCELLED),
        (QueryTimeout("x"), FailureKind.TIMEOUT),
        (TransientQueryError("x"), FailureKind.TRANSIENT),
        (ConflictError("x"), FailureKind.CONFLICT),
        (ValidationError("x"), FailureKind.PERMANENT),
        (OSError("x"), FailureKind.TRANSIENT),
        (OutOfMemoryQuery("x"), FailureKind.PERMANENT),
        (ParseError("x"), FailureKind.PERMANENT),
        (TokenError("x"), FailureKind.PERMANENT),
        (PermissionDenied("x"), FailureKind.PERMANENT),
        # The write fence's refusal: the run was ended with its worker, so the task is retried.
        (WriteFenceRefused(), FailureKind.TRANSIENT),
    ],
)
def test_classify(exc: BaseException | None, kind: FailureKind) -> None:
    """An exception maps to what happens next; no exception is a permanent failure."""
    assert classify(exc) is kind


def test_lease_keeper_renews_and_reports(world: World) -> None:
    """HELD keeps going; a cancel request and a lost lease set the event; drain releases."""
    _start(world)
    claimed = _claim(world)
    keeper = LeaseKeeper(
        world.session, claimed.task_run_id, "w1", claimed.attempt, lease_seconds=60, renew_seconds=0
    )
    keeper.renew_once()
    assert (keeper.cancel.is_set(), keeper.reason, keeper.lost) == (False, None, False)
    with world.session() as session:
        session.execute(update(Run).values(cancel_requested=True))
        session.commit()
    keeper.renew_once()
    assert (keeper.cancel.is_set(), keeper.reason) == (True, FailureKind.CANCELLED)
    other = LeaseKeeper(
        world.session, claimed.task_run_id, "w2", claimed.attempt, lease_seconds=60, renew_seconds=0
    )
    other.renew_once()
    assert (other.lost, other.cancel.is_set(), other.reason) == (True, True, None)
    zombie = LeaseKeeper(
        world.session,
        claimed.task_run_id,
        "w1",
        claimed.attempt + 1,
        lease_seconds=60,
        renew_seconds=0,
    )
    zombie.renew_once()
    assert (zombie.lost, zombie.cancel.is_set()) == (True, True)
    drain = threading.Event()
    drain.set()
    drained = LeaseKeeper(
        world.session,
        claimed.task_run_id,
        "w1",
        claimed.attempt,
        lease_seconds=60,
        renew_seconds=3600,
        drain=drain,
        drain_grace_seconds=0,
    )
    drained.renew_once()
    assert drained.reason is FailureKind.RELEASED
    drained.cancel.clear()
    drained._signal(FailureKind.CANCELLED)
    assert drained.reason is FailureKind.RELEASED, "the first reason wins"
    assert drained.cancel.is_set()


def test_lease_keeper_drain_grace_and_renewal_follow_a_controlled_clock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The time-dependent branches of `renew_once`, driven by a clock the test moves.

    Before this, the drain-grace-not-over and drain-already-seen branches were taken only when
    a real task ran for more than a couple of polls, and one run in a few thousand it did not.
    """
    now = [1000.0]
    monkeypatch.setattr(task_lease_module, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    renewals: list[float] = []

    def renewed(*args: object) -> LeaseState:
        renewals.append(now[0])
        return LeaseState.HELD

    monkeypatch.setattr(task_lease_module, "renew_lease", renewed)
    drain = threading.Event()
    keeper = LeaseKeeper(
        world.session,
        uuid.uuid4(),
        "w1",
        1,
        lease_seconds=60,
        renew_seconds=100,
        drain=drain,
        drain_grace_seconds=10,
    )
    keeper.renew_once()  # no drain, renewal not due
    assert (keeper.reason, renewals) == (None, [])
    drain.set()
    keeper.renew_once()  # first sight of the drain: the grace starts now
    assert (keeper.reason, keeper.cancel.is_set(), renewals) == (None, False, [])
    now[0] += 9.9
    keeper.renew_once()  # drain already seen, grace not over
    assert (keeper.reason, keeper.cancel.is_set(), renewals) == (None, False, [])
    now[0] += 0.1
    keeper.renew_once()  # the grace is over
    assert (keeper.reason, keeper.cancel.is_set()) == (FailureKind.RELEASED, True)
    assert renewals == []
    now[0] = 1100.0
    keeper.renew_once()  # renewal due, and the next one is a full interval away
    keeper.renew_once()
    assert renewals == [1100.0]
    now[0] = 1200.0
    keeper.renew_once()
    assert renewals == [1100.0, 1200.0]


def test_the_lease_keeper_thread_renews_once_per_wake_until_stopped(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_run` calls `renew_once` after every wake that is not the stop signal."""

    class Wakes:
        """A stop event whose waits answer from a script instead of the clock."""

        def __init__(self, answers: list[bool]) -> None:
            """Script the wakes.

            Parameters
            ----------
            answers
                What each successive `wait` returns.
            """
            self.answers = answers
            self.waited: list[float] = []

        def wait(self, timeout: float) -> bool:
            """Answer the next scripted wake.

            Parameters
            ----------
            timeout
                What the keeper asked to wait.

            Returns
            -------
            bool
                Whether the keeper was told to stop.
            """
            self.waited.append(timeout)
            return self.answers.pop(0)

    keeper = LeaseKeeper(world.session, uuid.uuid4(), "w1", 1, lease_seconds=60, renew_seconds=0)
    wakes = Wakes([False, False, True])
    calls: list[int] = []
    monkeypatch.setattr(keeper, "_stop", wakes)
    monkeypatch.setattr(keeper, "renew_once", lambda: calls.append(1))
    keeper._run()
    assert len(calls) == 2
    assert wakes.waited == [task_lease_module._POLL_SECONDS] * 3


def test_lease_keeper_survives_a_renewal_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database hiccup during renewal is logged and retried next time."""

    def broken(*args: object) -> LeaseState:
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(task_lease_module, "renew_lease", broken)
    keeper = LeaseKeeper(world.session, uuid.uuid4(), "w1", 1, lease_seconds=60, renew_seconds=0)
    keeper.renew_once()
    assert not keeper.cancel.is_set()


def test_lease_keeper_thread_starts_and_stops(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background thread renews until stopped."""
    _start(world)
    claimed = _claim(world)
    keeper = LeaseKeeper(
        world.session, claimed.task_run_id, "w1", claimed.attempt, lease_seconds=60, renew_seconds=0
    )
    renewals: list[int] = []
    real = task_lease_module.renew_lease

    def counting(
        session: Session, task_run_id: uuid.UUID, worker: str, attempt: int, lease_seconds: int
    ) -> LeaseState:
        renewals.append(attempt)
        return real(session, task_run_id, worker, attempt, lease_seconds)

    monkeypatch.setattr(task_lease_module, "renew_lease", counting)
    keeper.start()
    time.sleep(0.2)
    keeper.stop()
    keeper.stop()
    assert renewals and set(renewals) == {claimed.attempt}
    assert _task(world, "silver.clean").lease_expires_at is not None
    assert not keeper.cancel.is_set()
    assert not any(t.name == "lakana-lease" for t in threading.enumerate())
