"""What this worker is running right now, for the memory watchdog.

The watchdog runs in its own thread and must never wait for the main thread, so this
registry takes no lock: `dict` assignment and deletion are atomic, and a snapshot is a
single `tuple(...)` call.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskClaim:
    """A task run this worker holds the lease of.

    Parameters
    ----------
    task_run_id
        The task run.
    worker
        This worker's id.
    claim
        `ClaimedTask.claim`.
    """

    task_run_id: uuid.UUID
    worker: str
    claim: int


class Activity:
    """The query runs and task runs in flight in this process."""

    def __init__(self) -> None:
        self._queries: dict[uuid.UUID, None] = {}
        self._tasks: dict[TaskClaim, None] = {}
        self.stopping = False
        """Set by the watchdog before it kills the worker's children: the process is about to
        exit and the watchdog owns the outcome, so the main thread records nothing more."""

    @contextmanager
    def query(self, query_run_id: uuid.UUID) -> Generator[None, None, None]:
        """Mark a query run as executing for the duration of the block.

        Parameters
        ----------
        query_run_id
            The query run.

        Yields
        ------
        None
            While the run is marked.
        """
        self._queries[query_run_id] = None
        try:
            yield
        finally:
            self._queries.pop(query_run_id, None)

    @contextmanager
    def task(self, claim: TaskClaim) -> Generator[None, None, None]:
        """Mark a task run as executing for the duration of the block.

        Parameters
        ----------
        claim
            The task run and the claim this worker holds.

        Yields
        ------
        None
            While the task is marked.
        """
        self._tasks[claim] = None
        try:
            yield
        finally:
            self._tasks.pop(claim, None)

    def snapshot(self) -> tuple[tuple[uuid.UUID, ...], tuple[TaskClaim, ...]]:
        """Return what is in flight.

        Returns
        -------
        tuple
            The query run ids and the task claims.
        """
        return tuple(self._queries), tuple(self._tasks)


ACTIVITY = Activity()
"""The process-wide registry `run_one` and `run_claimed` report to."""
