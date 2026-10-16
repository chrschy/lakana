"""A non-blocking concurrency limiter for the governed append route.

`asyncio.Semaphore.acquire()` *waits* for a slot to free up rather than
refusing outright, which is the wrong behaviour for an HTTP request that
should fail fast with `503 Retry-After: 1` rather than queue indefinitely
behind other slow uploads — queuing would just move the pool-exhaustion and
memory-amplification risk `AppendConcurrencyLimiter` exists to bound
elsewhere. `AppendConcurrencyLimiter.try_acquire` never waits.
"""

from __future__ import annotations


class AppendConcurrencyLimiter:
    """A non-blocking counting limiter for concurrent governed appends.

    Safe with no internal lock: the only caller,
    `lakana_server.api.platform.tables.append_table_data`, is `async def`
    code running cooperatively on asyncio's single-threaded event loop, and
    neither `try_acquire` nor `release` ever `await` — so neither method
    can be interrupted mid-call by another coroutine checking or changing
    `_available` concurrently.

    Parameters
    ----------
    max_concurrent
        The most callers allowed to hold a slot at once.
    """

    def __init__(self, max_concurrent: int) -> None:
        self._available = max_concurrent

    def try_acquire(self) -> bool:
        """Attempt to claim one slot, without waiting for one to free up.

        Returns
        -------
        bool
            `True` if a slot was claimed — the caller must call `release`
            exactly once when it is done. `False` if none was available,
            leaving the count unchanged.
        """
        if self._available <= 0:
            return False
        self._available -= 1
        return True

    def release(self) -> None:
        """Return a slot previously claimed by a successful `try_acquire`."""
        self._available += 1
