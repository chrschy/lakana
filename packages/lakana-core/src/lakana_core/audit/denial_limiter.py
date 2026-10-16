"""A per-principal bound on `AUTHORIZATION_DENIED` audit rows.

`AuditingAuthorization` writes one row for every denial. Without a bound a principal that may do
almost nothing can still flood the audit log by asking for what it may not have, over and over.
`DenialAuditLimiter` caps the rows written per principal per minute, the way the server's
`AuthAuditLimiter` caps failed-authentication rows: a denial past the cap is still refused,
just not recorded (it is logged instead, `AuditingAuthorization`), and the number not recorded is
folded into the next row that is, or written as a summary row when the process stops (`drain`).

Two rules keep the bound from hiding a real event. The first denial of each action by a principal
is always recorded, so a principal that has used up its rate on one action still leaves a row for
every other action it was refused. And every principal has a bucket of its own, so nothing one
principal does uses up another's allowance.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

_MAX_PRINCIPALS = 10_000
"""Principals whose bucket is kept at once.

The oldest is dropped to make room for a new one. A dropped principal starts again with a full
bucket and no count of what was suppressed, so the bound can only ever cause *more* rows to be
written, never fewer; what is lost is the `suppressed` count of the dropped one."""

_MAX_KINDS = 50_000
"""(Principal, action) pairs remembered as already recorded once; the oldest is dropped first, so a
pair is recorded once more after it is dropped, never less than once."""


@dataclass
class _Bucket:
    """Token-bucket state of one principal.

    Parameters
    ----------
    tokens
        Rows this principal may still have recorded now, at most the per-minute rate.
    last_refill
        Monotonic time `tokens` was last brought up to date.
    suppressed
        Denials not recorded since the last row that was.
    """

    tokens: float
    last_refill: float
    suppressed: int = 0


class DenialAuditLimiter:
    """Decide whether a denial is written to the audit log.

    The state is in this process's memory: each server process and each worker has its own
    limiter, so a deployment with several processes records up to that many times the rate.
    A restart forgets the state, which can only record more. Thread-safe.

    Parameters
    ----------
    per_minute
        Rows recorded per principal per minute, and the size of the burst. The first denial of
        each action is recorded regardless.
    clock
        Monotonic time source; tests pass their own.
    max_principals
        Principals tracked at once (`_MAX_PRINCIPALS`).
    max_kinds
        (Principal, action) pairs tracked at once (`_MAX_KINDS`).
    """

    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        max_principals: int = _MAX_PRINCIPALS,
        max_kinds: int = _MAX_KINDS,
    ) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._max_principals = max_principals
        self._max_kinds = max_kinds
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._kinds: OrderedDict[tuple[str, str], None] = OrderedDict()

    def admit(self, principal: str, action: str) -> tuple[bool, int]:
        """Decide whether this denial is recorded.

        Parameters
        ----------
        principal
            The denied principal's id.
        action
            The denied action's name.

        Returns
        -------
        tuple of (bool, int)
            `(True, n)` to record it, where `n` is how many of this principal's denials were not
            recorded since its last row (reset to 0 by this call); `(False, 0)` not to, having
            counted it as suppressed.
        """
        now = self._clock()
        with self._lock:
            first_of_kind = (principal, action) not in self._kinds
            if first_of_kind:
                self._kinds[principal, action] = None
                if len(self._kinds) > self._max_kinds:
                    self._kinds.popitem(last=False)
            bucket = self._buckets.get(principal)
            if bucket is None:
                if len(self._buckets) >= self._max_principals:
                    self._buckets.popitem(last=False)
                bucket = _Bucket(tokens=float(self._per_minute), last_refill=now)
                self._buckets[principal] = bucket
            else:
                elapsed = now - bucket.last_refill
                bucket.tokens = min(
                    float(self._per_minute), bucket.tokens + elapsed * self._per_minute / 60.0
                )
                bucket.last_refill = now
            if bucket.tokens >= 1.0 or first_of_kind:
                bucket.tokens = max(bucket.tokens - 1.0, 0.0)
                suppressed = bucket.suppressed
                bucket.suppressed = 0
                return True, suppressed
            bucket.suppressed += 1
            return False, 0

    def drain(self) -> dict[str, int]:
        """Hand back the denials that no recorded row has carried yet, and forget them.

        Called when the process stops, so that a count which would otherwise be lost with the
        process can be written as a summary row.

        Returns
        -------
        dict of str to int
            For each principal id with a count above 0, the number of denials not recorded since
            its last row.
        """
        with self._lock:
            pending = {
                principal: bucket.suppressed
                for principal, bucket in self._buckets.items()
                if bucket.suppressed > 0
            }
            for principal in pending:
                self._buckets[principal].suppressed = 0
        return pending

    def sizes(self) -> tuple[int, int]:
        """Report how much state is held, for tests of the bounds.

        Returns
        -------
        tuple of (int, int)
            The number of principals and of (principal, action) pairs tracked.
        """
        with self._lock:
            return len(self._buckets), len(self._kinds)
