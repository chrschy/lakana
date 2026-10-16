"""Per-client rate limiting for failed-authentication audit writes.

Without a bound, an attacker retrying a bad credential causes `get_principal`
(`lakana_server.deps`) to write one `AUTHENTICATION_FAILED` audit row per
attempt, flooding the audit log and, at volume, the database it lives in.
`AuthAuditLimiter` is a per-client token bucket that caps how many of those
rows get written per minute; requests past the cap are still rejected with
401, just not recorded individually — the number suppressed is folded into
the next row that *is* recorded, so the flood is still visible, just not
one row per attempt.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

_MAX_CLIENTS = 10_000
"""Bound on distinct client keys tracked at once.

A client key is attacker-influenced (`request.client.host`, one per source
IP an attacker can open a connection from), so the per-client dict must not
grow without bound. The oldest tracked client is evicted to make room for a
new one past this bound, trading that client's burst allowance for a fresh
one — acceptable, since the bound only matters under an attack large enough
to cycle through 10 000 distinct source addresses.
"""


@dataclass
class _Bucket:
    """Token-bucket state tracked for one client key.

    Parameters
    ----------
    tokens
        Tokens currently available to this client, capped at the
        configured per-minute rate.
    last_refill
        Monotonic timestamp `tokens` was last brought up to date.
    suppressed
        Failed-authentication attempts refused for this client since the
        last one that was admitted.
    """

    tokens: float
    last_refill: float
    suppressed: int = 0


class AuthAuditLimiter:
    """Per-client token bucket bounding failed-authentication audit writes.

    Each client key gets its own bucket, refilling `per_minute` tokens per
    60 seconds up to a capacity of `per_minute` — so a client can burst up
    to `per_minute` admits immediately, then sustains `per_minute` per
    minute thereafter. Thread-safe via an internal lock: FastAPI runs sync
    dependencies such as `get_principal` in a threadpool, so concurrent
    requests from different clients call `admit` concurrently.

    Parameters
    ----------
    per_minute
        Tokens available to each client per 60 seconds, and the bucket's
        capacity.
    clock
        Monotonic time source. Injectable so tests can advance time
        deterministically instead of sleeping.
    """

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def admit(self, client: str) -> tuple[bool, int]:
        """Decide whether a failed-authentication attempt may be recorded.

        Parameters
        ----------
        client
            The requesting client's key (`request.client.host`).

        Returns
        -------
        tuple of (bool, int)
            `(True, n)` when admitted, where `n` is the number of attempts
            suppressed for this client since the last one admitted (reset
            to 0 by this call). `(False, 0)` when refused, having
            incremented this client's suppressed count.
        """
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(client)
            if bucket is None:
                if len(self._buckets) >= _MAX_CLIENTS:
                    self._buckets.popitem(last=False)
                bucket = _Bucket(tokens=float(self._per_minute), last_refill=now)
                self._buckets[client] = bucket
            else:
                elapsed = now - bucket.last_refill
                bucket.tokens = min(
                    float(self._per_minute),
                    bucket.tokens + elapsed * self._per_minute / 60.0,
                )
                bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                suppressed = bucket.suppressed
                bucket.suppressed = 0
                return True, suppressed

            bucket.suppressed += 1
            return False, 0
