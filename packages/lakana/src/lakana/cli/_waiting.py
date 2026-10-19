"""The line on standard error that says why a query or a deploy still waits."""

from __future__ import annotations

import time
from collections.abc import Callable

import typer

from lakana._models import Deploy, QueryStatus
from lakana.cli import _output
from lakana.cli._state import CliState

WAIT_LINE_AFTER_SECONDS = 3.0
"""How long a query or deploy must have been queued before the commands say why it waits."""

WAIT_LINE_LIMIT = 400
"""The most characters of a reason's sentence the wait line keeps, before a ``…``."""


def clock() -> float:
    """Read the clock the wait line counts its seconds on (a seam for the tests).

    Returns
    -------
    float
        A monotonic time in seconds.
    """
    return time.monotonic()


class WaitLine:
    """Say on standard error why a query or a deploy still waits, once per reason.

    Used as the `on_update` of `QueryRun.wait` and `Deploys.wait`: it prints
    ``Waiting: <message>`` after the query or deploy has been queued for
    `WAIT_LINE_AFTER_SECONDS`, and again only when the reason's *code* changes. The numbers in
    the sentence change on every poll and never print it again, and the decision never reads the
    sentence. Nothing is printed with ``--output json``, for a status that is not QUEUED or has
    no reason, or for an empty sentence. The seconds count from the first QUEUED status this
    command saw, so the CLI compares no clock of its own with the server's. Standard output is
    never touched.

    Parameters
    ----------
    state
        The CLI state (for the output mode).
    monotonic
        The clock, in seconds.
    """

    def __init__(self, state: CliState, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._state = state
        self._monotonic = monotonic
        self._queued_since: float | None = None
        self._printed: str | None = None

    def __call__(self, status: QueryStatus | Deploy) -> None:
        """Print the reason if it is time and it is a new one.

        Parameters
        ----------
        status
            The status just polled: a query's (``QUEUED``) or a deploy run (``queued``).
        """
        if self._state.output == "json" or status.status.upper() != "QUEUED":
            return
        now = self._monotonic()
        if self._queued_since is None:
            self._queued_since = now
        reason = status.waiting_for
        if reason is None or now - self._queued_since < WAIT_LINE_AFTER_SECONDS:
            return
        if reason.code == self._printed or not reason.message.strip():
            return
        self._printed = reason.code
        message = _output.inline(reason.message, WAIT_LINE_LIMIT)
        typer.echo(_output.safe_text(f"Waiting: {message}"), err=True)
