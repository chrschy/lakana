"""The readiness check behind `GET /readyz`: the database, the storage and a worker.

The route needs no sign-in, so it is built so that it cannot be used against the instance:

- **It reveals names only.** The answer lists which of `database`, `storage` and `worker`
  failed; never a host, an environment, an error text or a version. The reasons go to the log.
- **It cannot amplify load.** One answer is reused for `LAKANA_READINESS__CACHE_SECONDS`, and
  requests that arrive while a check runs wait for that one check. However often the route is
  asked, a server process reads the database once and asks each storage once per period.
- **It is bounded in time and in threads.** The database read and the storage probes each get
  `LAKANA_READINESS__TIMEOUT_SECONDS`. A check runs in a daemon thread of its own; one that
  is still running from an earlier request (a hanging connection) counts as failed and is not
  started a second time, so a hanging database or storage holds at most one thread each.

There is no rate limit on top: past the cache a request costs what `/healthz` costs, and a
limit would make a monitoring system's own probes fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

from lakana_core.identifiers import shown
from lakana_core.readiness import READINESS_CHECKS, DatabaseFindings
from lakana_core.storage.resolver import ResolvedStorage

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_NOT_CHECKED = "not checked, because the database did not answer"
"""The reason given for the storage and the worker when the database failed."""


class _StillRunning(Exception):
    """The same check, started by an earlier request, has not ended yet."""


class ReadinessCheck:
    """Run the readiness checks with a time limit each, and reuse the answer for a while.

    Parameters
    ----------
    read
        Reads the database (`lakana_core.readiness.read_database`, its arguments bound).
    probe
        Probes one storage (`lakana_core.readiness.probe_storage`).
    timeout_seconds
        The time limit of the database read, and of the storage probes together.
    cache_seconds
        How long one answer is reused.
    clock
        Monotonic seconds; `time.monotonic` by default.
    """

    def __init__(
        self,
        *,
        read: Callable[[], DatabaseFindings],
        probe: Callable[[ResolvedStorage], None],
        timeout_seconds: float,
        cache_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._probe = probe
        self._timeout = timeout_seconds
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cached: tuple[float, tuple[str, ...]] | None = None
        self._running: dict[str, threading.Event] = {}
        """Set when the work of that key has ended; set before its outcome reaches the loop."""
        self._reported: tuple[str, ...] = ()

    async def failed(self) -> tuple[str, ...]:
        """Say which checks fail now, from the cache when the last answer is recent enough.

        Returns
        -------
        tuple of str
            The failed checks, in the order of `READINESS_CHECKS`; empty when ready.
        """
        async with self._lock:
            cached = self._cached
            if cached is not None and self._clock() - cached[0] < self._cache_seconds:
                return cached[1]
            reasons = await self._evaluate()
            failed = tuple(name for name in READINESS_CHECKS if name in reasons)
            self._cached = (self._clock(), failed)
            self._report(failed, reasons)
            return failed

    async def _evaluate(self) -> dict[str, str]:
        """Run the checks once.

        Returns
        -------
        dict of str to str
            The reason of each failed check, by its name; for the log only.
        """
        self._running = {key: e for key, e in self._running.items() if not e.is_set()}
        try:
            findings = await self._within_time("database", self._read)
        except Exception as exc:
            return {
                "database": _reason(exc, self._timeout),
                "storage": _NOT_CHECKED,
                "worker": _NOT_CHECKED,
            }
        reasons: dict[str, str] = {}
        if not findings.worker_alive:
            reasons["worker"] = "no worker has written a heartbeat within the lease"
        broken = await self._probe_all(findings)
        if broken:
            reasons["storage"] = "; ".join(broken)
        return reasons

    async def _probe_all(self, findings: DatabaseFindings) -> list[str]:
        """Probe every storage at once, within one time limit.

        Parameters
        ----------
        findings
            What the database read found.

        Returns
        -------
        list of str
            One reason per environment whose storage failed.
        """
        broken: list[str] = []
        probes: dict[str, asyncio.Future[None]] = {}
        for target in findings.storages:
            storage = target.storage
            if storage is None:
                broken.append(
                    f"environment {shown(target.environment)}: it does not exist, or its storage "
                    "secret or the master key is missing"
                )
                continue
            started = self._start(f"storage:{target.environment}", lambda s=storage: self._probe(s))
            if started is None:
                broken.append(
                    f"environment {shown(target.environment)}: the probe started earlier has "
                    "not ended"
                )
            else:
                probes[target.environment] = started
        if probes:
            await asyncio.wait(probes.values(), timeout=self._timeout)
        for environment, future in probes.items():
            if not future.done():
                future.cancel()
                broken.append(
                    f"environment {shown(environment)}: no answer within {self._timeout:g} s"
                )
            elif (error := future.exception()) is not None:
                broken.append(f"environment {shown(environment)}: {type(error).__name__}")
        return broken

    async def _within_time(self, key: str, work: Callable[[], _T]) -> _T:
        """Run one piece of work in its own thread and wait for it, up to the time limit.

        Parameters
        ----------
        key
            Names the work; the same key is not started twice at once.
        work
            What to run.

        Returns
        -------
        object
            What `work` returned.

        Raises
        ------
        _StillRunning
            If the same work started earlier is still running.
        """
        started = self._start(key, work)
        if started is None:
            raise _StillRunning
        return await asyncio.wait_for(started, self._timeout)

    def _start(self, key: str, work: Callable[[], _T]) -> asyncio.Future[_T] | None:
        """Start one piece of work in a daemon thread, unless it is still running.

        Parameters
        ----------
        key
            Names the work.
        work
            What to run.

        Returns
        -------
        asyncio.Future or None
            Settles with the work's result or error; `None` when the same work is still
            running from an earlier check.
        """
        if key in self._running and not self._running[key].is_set():
            return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_T] = loop.create_future()
        ended = threading.Event()

        def run() -> None:
            """Run the work, mark it ended, and hand its outcome to the event loop."""
            try:
                outcome: tuple[_T | None, BaseException | None] = (work(), None)
            except Exception as exc:
                outcome = (None, exc)
            ended.set()
            # The loop may be closed when a hanging check ends late; nobody waits then.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_settle, future, *outcome)

        self._running[key] = ended
        threading.Thread(target=run, name=f"lakana-readiness-{key}", daemon=True).start()
        return future

    def _report(self, failed: tuple[str, ...], reasons: dict[str, str]) -> None:
        """Log a change of the answer: once when it starts failing, once when it is back.

        Parameters
        ----------
        failed
            The failed checks.
        reasons
            Their reasons.
        """
        if failed == self._reported:
            return
        self._reported = failed
        if failed:
            logger.warning(
                "The instance is not ready: /readyz answers 503. %s.",
                "; ".join(f"{name}: {reasons[name]}" for name in failed),
            )
        else:  # it failed before, or the answer would not have changed
            logger.info("The instance is ready again: /readyz answers 200.")


def _settle(future: asyncio.Future[_T], result: _T | None, error: BaseException | None) -> None:
    """Hand a thread's outcome to its future, unless nobody waits for it any more.

    Parameters
    ----------
    future
        The future of the work.
    result
        What the work returned.
    error
        What it raised, or `None`.
    """
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)  # ty: ignore[invalid-argument-type]


def _reason(exc: BaseException, timeout: float) -> str:
    """Say in a few words why a check failed, without the error's own text.

    Parameters
    ----------
    exc
        What the check raised.
    timeout
        Its time limit.

    Returns
    -------
    str
        The reason, for the log: an error's text can name a host or a password.
    """
    if isinstance(exc, TimeoutError):
        return f"no answer within {timeout:g} s"
    if isinstance(exc, _StillRunning):
        return "the check started earlier has not ended"
    return type(exc).__name__
