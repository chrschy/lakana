"""Tests for how the readiness route runs its checks: time limits, the cache and the log."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from typing import cast

import pytest

from lakana_core.readiness import DatabaseFindings, StorageTarget
from lakana_core.storage.resolver import ResolvedStorage
from lakana_server.readiness import ReadinessCheck, _settle

_STORAGE = cast(ResolvedStorage, object())
"""A stand-in storage: the probes of these tests never look at it."""


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        """Read the time.

        Returns
        -------
        float
            The current reading.
        """
        return self.now


@pytest.fixture
def gate() -> Iterator[threading.Event]:
    """Provide an event that a hanging check waits on, set when the test ends.

    Yields
    ------
    threading.Event
        The event; set afterwards so no thread is left waiting.
    """
    event = threading.Event()
    yield event
    event.set()


def _findings(*, alive: bool = True, storages: int = 1) -> DatabaseFindings:
    """Build what a database read finds.

    Parameters
    ----------
    alive
        Whether a worker is alive.
    storages
        How many environments with a storage to probe.

    Returns
    -------
    DatabaseFindings
        The findings.
    """
    return DatabaseFindings(
        worker_alive=alive,
        storages=tuple(StorageTarget(f"env{i}", _STORAGE) for i in range(storages)),
    )


def _check(
    read: Callable[[], DatabaseFindings],
    probe: Callable[[ResolvedStorage], None] = lambda storage: None,
    clock: _Clock | None = None,
) -> ReadinessCheck:
    """Build a check with short time limits.

    Parameters
    ----------
    read
        The database read.
    probe
        The storage probe.
    clock
        The clock of the cache; a fresh `_Clock` when `None`.

    Returns
    -------
    ReadinessCheck
        A check with a 0.2 s time limit and a 5 s cache.
    """
    return ReadinessCheck(
        read=read,
        probe=probe,
        timeout_seconds=0.2,
        cache_seconds=5.0,
        clock=clock or _Clock(),
    )


def _failed(check: ReadinessCheck) -> tuple[str, ...]:
    """Run one check in a fresh event loop.

    Parameters
    ----------
    check
        The check.

    Returns
    -------
    tuple of str
        The names of the checks that failed.
    """
    return asyncio.run(check.failed())


def test_everything_answering_fails_nothing() -> None:
    assert _failed(_check(lambda: _findings())) == ()


def test_no_environment_yet_leaves_nothing_to_probe() -> None:
    assert _failed(_check(lambda: _findings(storages=0))) == ()


def test_no_alive_worker_fails_the_worker() -> None:
    assert _failed(_check(lambda: _findings(alive=False))) == ("worker",)


def test_a_failed_database_fails_all_three(caplog: pytest.LogCaptureFixture) -> None:
    def read() -> DatabaseFindings:
        """Fail as a database that refuses the connection does.

        Raises
        ------
        ConnectionRefusedError
            Always.
        """
        raise ConnectionRefusedError("postgres:5432 refused")

    with caplog.at_level(logging.WARNING, logger="lakana_server.readiness"):
        assert _failed(_check(read)) == ("database", "storage", "worker")
    (record,) = caplog.records
    assert "ConnectionRefusedError" in record.getMessage()
    assert "postgres:5432" not in record.getMessage()


def test_a_storage_that_cannot_be_set_up_fails_the_storage() -> None:
    findings = DatabaseFindings(worker_alive=True, storages=(StorageTarget("dev", None),))

    assert _failed(_check(lambda: findings)) == ("storage",)


def test_a_storage_that_raises_fails_the_storage() -> None:
    def probe(storage: ResolvedStorage) -> None:
        """Fail as an unreachable storage does.

        Parameters
        ----------
        storage
            Ignored.

        Raises
        ------
        OSError
            Always.
        """
        raise OSError("AWS Error NETWORK_CONNECTION")

    assert _failed(_check(lambda: _findings(storages=2), probe)) == ("storage",)


def test_a_hanging_database_fails_in_time_and_is_not_asked_again_while_it_hangs(
    gate: threading.Event,
) -> None:
    calls: list[int] = []

    def read() -> DatabaseFindings:
        """Hang until the test ends.

        Returns
        -------
        DatabaseFindings
            Once released.
        """
        calls.append(1)
        gate.wait(10)
        return _findings()

    clock = _Clock()
    check = _check(read, clock=clock)

    assert _failed(check) == ("database", "storage", "worker")
    clock.now += 6
    assert _failed(check) == ("database", "storage", "worker")
    assert len(calls) == 1


def test_a_hanging_storage_fails_in_time_and_is_not_asked_again_while_it_hangs(
    gate: threading.Event,
) -> None:
    calls: list[int] = []

    def probe(storage: ResolvedStorage) -> None:
        """Hang until the test ends.

        Parameters
        ----------
        storage
            Ignored.
        """
        calls.append(1)
        gate.wait(10)

    clock = _Clock()
    check = _check(lambda: _findings(alive=False), probe, clock=clock)

    assert _failed(check) == ("storage", "worker")
    clock.now += 6
    assert _failed(check) == ("storage", "worker")
    assert len(calls) == 1


def test_an_outcome_that_arrives_after_the_time_limit_is_dropped() -> None:
    async def settle_late() -> bool:
        """Settle a future that the waiter already gave up on.

        Returns
        -------
        bool
            Whether the future stayed cancelled.
        """
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        future.cancel()
        _settle(future, 1, None)
        await asyncio.sleep(0)
        return future.cancelled()

    assert asyncio.run(settle_late()) is True


def test_an_answer_is_reused_until_the_cache_runs_out() -> None:
    calls: list[int] = []

    def read() -> DatabaseFindings:
        """Count the reads.

        Returns
        -------
        DatabaseFindings
            Everything answers.
        """
        calls.append(1)
        return _findings()

    clock = _Clock()
    check = _check(read, clock=clock)

    _failed(check)
    clock.now += 4.9
    _failed(check)
    assert len(calls) == 1
    clock.now += 0.2
    _failed(check)
    assert len(calls) == 2


def test_requests_at_the_same_time_share_one_check() -> None:
    calls: list[int] = []

    def read() -> DatabaseFindings:
        """Count the reads.

        Returns
        -------
        DatabaseFindings
            Everything answers.
        """
        calls.append(1)
        return _findings()

    check = _check(read)

    async def ask_five() -> list[tuple[str, ...]]:
        """Ask five times at once.

        Returns
        -------
        list of tuple of str
            The five answers.
        """
        return await asyncio.gather(*(check.failed() for _ in range(5)))

    assert asyncio.run(ask_five()) == [()] * 5
    assert len(calls) == 1


def test_the_log_says_when_readiness_fails_and_when_it_is_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    alive = [False]
    clock = _Clock()
    check = _check(lambda: _findings(alive=alive[0]), clock=clock)

    with caplog.at_level(logging.INFO, logger="lakana_server.readiness"):
        _failed(check)
        clock.now += 6
        _failed(check)
        alive[0] = True
        clock.now += 6
        _failed(check)
        clock.now += 6
        _failed(check)

    assert [record.levelno for record in caplog.records] == [logging.WARNING, logging.INFO]
    warning, info = (record.getMessage() for record in caplog.records)
    assert "/readyz" in warning and "worker" in warning
    assert "ready again" in info
