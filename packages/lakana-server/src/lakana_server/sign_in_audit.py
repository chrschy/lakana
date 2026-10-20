"""At most one `AUTHENTICATION_SUCCEEDED` audit row per credential and window.

Every authenticated request used to write a row with its own commit, so a screen polling every
three seconds wrote about 28,800 rows a day, and audit rows are never deleted.
`SignInAuditLimiter` lets `get_principal` (`lakana_server.deps`) record the first success of a
credential, then none for the same credential until `LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES` have
passed since that row; the number left out is carried by the next row that is written, as
`detail.suppressed`, the way `AuthAuditLimiter` and `DenialAuditLimiter` carry theirs.

A credential is a token kind and its key id (`("pat", key_id)` or `("task", key_id)`); the
principal it authenticated is kept with it. Every rule below can only cause *more* rows, never
fewer:

- another credential, another kind, or the same key id authenticating another principal, is
  recorded on its own;
- a failed attempt that names the credential (`forget`) makes its next success recorded, so a
  token that worked again after it was refused, for example after its principal was reactivated,
  leaves a row;
- a row that could not be written (`not_recorded`) makes the next success recorded, carrying
  this one in its count;
- a credential pushed out of the bounded memory is treated as never seen, and recorded again.

The state is in this process's memory: each server process records a credential once per window,
so several server processes behind one address write up to that many rows. A restart forgets it,
which records the next success of every credential. The counts not yet carried by a row when the
process stops are not written.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

_MAX_CREDENTIALS = 10_000
"""Credentials whose window is kept at once.

Only credentials that verified are added, so a caller without a working token cannot fill it.
The least recently used is dropped to make room; a dropped one is recorded at its next success,
and its count of suppressed successes is lost."""

Credential = tuple[str, str]
"""A token's kind (``"pat"`` or ``"task"``) and its key id."""


@dataclass
class _Window:
    """What is known of one credential.

    Parameters
    ----------
    principal_id
        The principal the credential authenticated when its last row was written.
    recorded_at
        Monotonic time of that row, or `None` when the next success must be recorded.
    suppressed
        Successes not recorded since that row.
    """

    principal_id: uuid.UUID
    recorded_at: float | None
    suppressed: int = 0


class SignInAuditLimiter:
    """Decide whether a successful authentication is written to the audit log.

    Thread-safe: FastAPI runs `get_principal` in a threadpool.

    Parameters
    ----------
    window_seconds
        Seconds after a recorded success during which the same credential writes no second row;
        0 records every success and keeps no state.
    clock
        Monotonic time source; tests pass their own.
    max_credentials
        Credentials tracked at once (`_MAX_CREDENTIALS`).
    """

    def __init__(
        self,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_credentials: int = _MAX_CREDENTIALS,
    ) -> None:
        self.window_seconds = window_seconds
        self._clock = clock
        self._max_credentials = max_credentials
        self._lock = threading.Lock()
        self._windows: OrderedDict[Credential, _Window] = OrderedDict()

    def admit(self, credential: Credential, principal_id: uuid.UUID) -> tuple[bool, int]:
        """Decide whether this success is recorded.

        Parameters
        ----------
        credential
            The token's kind and key id.
        principal_id
            The principal the token authenticated.

        Returns
        -------
        tuple of (bool, int)
            `(True, n)` to record it, where `n` is how many successes of this credential were not
            recorded since its last row (reset to 0 by this call); `(False, 0)` not to, having
            counted it.
        """
        if self.window_seconds <= 0:
            return True, 0
        now = self._clock()
        with self._lock:
            window = self._windows.get(credential)
            if window is not None:
                self._windows.move_to_end(credential)
            if (
                window is not None
                and window.principal_id == principal_id
                and window.recorded_at is not None
                and now - window.recorded_at < self.window_seconds
            ):
                window.suppressed += 1
                return False, 0
            carried = (
                window.suppressed
                if window is not None and window.principal_id == principal_id
                else 0
            )
            self._windows[credential] = _Window(principal_id=principal_id, recorded_at=now)
            if len(self._windows) > self._max_credentials:
                self._windows.popitem(last=False)
            return True, carried

    def forget(self, credential: Credential) -> None:
        """Make the next success of a credential recorded, after a failed attempt that named it.

        Parameters
        ----------
        credential
            The kind and key id the failed attempt presented. One never admitted adds nothing.
        """
        with self._lock:
            window = self._windows.get(credential)
            if window is not None:
                window.recorded_at = None

    def not_recorded(self, credential: Credential, suppressed: int) -> None:
        """Undo an admission whose row could not be written.

        The next success is recorded and its count includes this one and the `suppressed` the
        failed row would have carried.

        Parameters
        ----------
        credential
            The credential `admit` admitted.
        suppressed
            The count `admit` returned with it.
        """
        with self._lock:
            window = self._windows.get(credential)
            if window is not None:
                window.recorded_at = None
                window.suppressed += suppressed + 1

    def size(self) -> int:
        """Report how many credentials are tracked, for tests of the bound.

        Returns
        -------
        int
            The number of credentials held.
        """
        with self._lock:
            return len(self._windows)
