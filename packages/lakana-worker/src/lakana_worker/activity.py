"""What this worker is running right now, for the memory watchdog.

The watchdog runs in its own thread and must never wait for the main thread, so this
registry takes no lock: `dict` assignment and deletion are atomic, and a snapshot is a
single `tuple(...)` call.

Once the worker is stopping, nothing leaves the registry. The watchdog marks the worker
stopping, kills its children and only then records what was in flight; a thread whose child
died leaves its block in between without recording anything (it sees `Activity.stopping`), and
without this the recording would not find its task, which would stay ``running`` until the
lease ran out (#154). An entry that stays although its thread recorded its outcome itself is
harmless: the recording is conditional on the run still being ``RUNNING`` under that claim.
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
        self._left_queries: dict[uuid.UUID, None] = {}
        self._left_tasks: dict[TaskClaim, None] = {}
        self._stopping = False

    @property
    def stopping(self) -> bool:
        """Set by the watchdog before it kills the worker's children.

        The process is about to exit and the watchdog owns the outcome, so the main thread
        records nothing more, and what leaves the registry from then on stays in its snapshot.
        Clearing it (a new process, in tests) drops what left while it was set; the clear is
        not atomic with a block that ends at the same moment, so its caller must have no thread
        left inside a block. Production never clears it.

        Returns
        -------
        bool
            Whether the worker is stopping.
        """
        return self._stopping

    @stopping.setter
    def stopping(self, value: bool) -> None:
        """Mark the worker stopping, or start afresh.

        Parameters
        ----------
        value
            Whether the worker is stopping.
        """
        self._stopping = value
        if not value:
            for query_run_id in tuple(self._left_queries):
                self._queries.pop(query_run_id, None)
            for claim in tuple(self._left_tasks):
                self._tasks.pop(claim, None)
            self._left_queries.clear()
            self._left_tasks.clear()

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
            if self._stopping:
                self._left_queries[query_run_id] = None
            else:
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
            if self._stopping:
                self._left_tasks[claim] = None
            else:
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
