"""Run resources: `Client.runs`, `Client.scheduler` and `Client.workers`."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import (
    Notification,
    Run,
    RunTask,
    SchedulerStatus,
    TaskAttempt,
    TaskLog,
    WorkerInfo,
)
from lakana._routes import ROUTES
from lakana.errors import RunFailed
from lakana.resources.checks import check_result_from_json
from lakana.resources.queries import waiting_for_from_json

_FINISHED = frozenset({"succeeded", "failed", "cancelled"})
_RunList = list[Run]
_WorkerList = list[WorkerInfo]


def _when(value: str | None) -> datetime | None:
    """Parse an optional ISO timestamp.

    Parameters
    ----------
    value
        The timestamp or `None`.

    Returns
    -------
    datetime or None
        The parsed value.
    """
    return datetime.fromisoformat(value) if value is not None else None


def notification_from_json(data: dict[str, Any]) -> Notification:
    """Build a `Notification` from a `NotificationOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    Notification
        The notification.
    """
    return Notification(
        id=data["id"],
        event=data["event"],
        state=data["state"],
        attempts=data["attempts"],
        max_attempts=data["max_attempts"],
        last_result=data["last_result"],
        delivered_at=_when(data["delivered_at"]),
    )


def run_from_json(data: dict[str, Any]) -> Run:
    """Build a `Run` from a `RunOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    Run
        The run.
    """
    return Run(
        id=data["id"],
        environment=data["environment"],
        repository=data["repository"],
        project_version_id=data["project_version_id"],
        commit_sha=data["commit_sha"],
        kind=data["kind"],
        status=data["status"],
        cancel_requested=data["cancel_requested"],
        triggered_by=data["triggered_by"],
        run_as=data["run_as"],
        resumed_from_run_id=data["resumed_from_run_id"],
        selection=data["selection"],
        error_message=data["error_message"],
        created_at=datetime.fromisoformat(data["created_at"]),
        started_at=_when(data["started_at"]),
        finished_at=_when(data["finished_at"]),
        tasks=[
            RunTask(
                asset=t["asset"],
                status=t["status"],
                attempt=t["attempt"],
                max_attempts=t["max_attempts"],
                timeout_seconds=t["timeout_seconds"],
                next_attempt_at=_when(t["next_attempt_at"]),
                started_at=_when(t["started_at"]),
                finished_at=_when(t["finished_at"]),
                error_message=t["error_message"],
                query_run_id=t["query_run_id"],
                rows_written=t["rows_written"],
                waiting_for=waiting_for_from_json(t.get("waiting_for")),
                kind=t.get("kind", "asset"),
                checks=tuple(check_result_from_json(c) for c in t.get("checks", ())),
                kept_check=t.get("kept_check", False),
                setting_back=t.get("setting_back", False),
            )
            for t in data["tasks"]
        ],
        job=data.get("job"),
        trigger=data.get("trigger"),
        scheduled_for=_when(data.get("scheduled_for")),
        missed=data.get("missed", 0),
        notifications=[notification_from_json(n) for n in data.get("notifications", ())],
    )


class Runs:
    """Start, follow, cancel and resume runs.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def start(
        self,
        environment: str,
        repository: str,
        *,
        select: Sequence[str] = (),
        upstream: bool = False,
        downstream: bool = False,
    ) -> Run:
        """Start a run of the deployed project version (needs `RUN`).

        Parameters
        ----------
        environment
            Environment name.
        repository
            Repository name.
        select
            Shell-glob patterns over dotted table names; empty runs everything.
        upstream
            Add every ancestor of the matched assets.
        downstream
            Add every descendant of the matched assets.

        Returns
        -------
        Run
            The queued run.

        Raises
        ------
        lakana.errors.Conflict
            If a run is already active (its id is in the message), or no run
            principal is bound in a protected environment.
        lakana.errors.ValidationError
            If a pattern matches nothing.
        """
        response = self._transport.request(
            ROUTES["runs.start"],
            path_params={"env": environment},
            json={
                "repository": repository,
                "select": list(select),
                "upstream": upstream,
                "downstream": downstream,
            },
        )
        return run_from_json(response.json())

    def list(
        self, environment: str, *, repository: str | None = None, limit: int | None = None
    ) -> _RunList:
        """List runs, newest first, without tasks.

        Parameters
        ----------
        environment
            Environment name.
        repository
            Only this repository's runs.
        limit
            Most runs returned (1 to 200); `None` leaves it to the server (100).

        Returns
        -------
        list of Run
            Newest first.

        Raises
        ------
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200.
        """
        params: dict[str, str | int] = {}
        if repository is not None:
            params["repository"] = repository
        if limit is not None:
            params["limit"] = limit
        response = self._transport.request(
            ROUTES["runs.list"], path_params={"env": environment}, params=params or None
        )
        return [run_from_json(item) for item in response.json()]

    def get(self, environment: str, run_id: str) -> Run:
        """Show one run with its tasks.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.

        Returns
        -------
        Run
            The run.
        """
        response = self._transport.request(
            ROUTES["runs.get"], path_params={"env": environment, "run_id": run_id}
        )
        return run_from_json(response.json())

    def cancel(self, environment: str, run_id: str) -> Run:
        """Request cancellation.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.

        Returns
        -------
        Run
            The run with `cancel_requested`.
        """
        response = self._transport.request(
            ROUTES["runs.cancel"], path_params={"env": environment, "run_id": run_id}
        )
        return run_from_json(response.json())

    def resume(self, environment: str, run_id: str) -> Run:
        """Start a new run over a finished run's failed, upstream_failed and cancelled tasks.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The finished run.

        Returns
        -------
        Run
            The new run.
        """
        response = self._transport.request(
            ROUTES["runs.resume"], path_params={"env": environment, "run_id": run_id}
        )
        return run_from_json(response.json())

    def log(self, environment: str, run_id: str, asset: str) -> TaskLog:
        """Return a task's free-text log and attempts.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.
        asset
            The task's dotted table name.

        Returns
        -------
        TaskLog
            Text and attempts.
        """
        response = self._transport.request(
            ROUTES["runs.log"],
            path_params={"env": environment, "run_id": run_id, "asset": asset},
        )
        data = response.json()
        return TaskLog(
            run_id=data["run_id"],
            asset=data["asset"],
            text=data["text"],
            attempts=[
                TaskAttempt(
                    attempt=a["attempt"],
                    worker=a["worker"],
                    started_at=datetime.fromisoformat(a["started_at"]),
                    finished_at=_when(a["finished_at"]),
                    outcome=a["outcome"],
                    rows_written=a["rows_written"],
                    error_message=a["error_message"],
                )
                for a in data["attempts"]
            ],
        )

    def wait(
        self,
        environment: str,
        run_id: str,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        max_interval: float = 5.0,
        on_update: Callable[[Run], None] | None = None,
    ) -> Run:
        """Poll a run until it finishes.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.
        timeout
            Seconds to keep polling, or `None` for no limit.
        poll_interval
            First interval, doubling up to `max_interval`.
        max_interval
            Largest interval.
        on_update
            Called with every polled state (progress display).

        Returns
        -------
        Run
            The run, once `succeeded`.

        Raises
        ------
        lakana.errors.RunFailed
            If it ends `failed` or `cancelled`.
        TimeoutError
            If `timeout` elapses first; the run keeps going server-side.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        interval = poll_interval
        while True:
            run = self.get(environment, run_id)
            if on_update is not None:
                on_update(run)
            if run.status == "succeeded":
                return run
            if run.status in _FINISHED:
                raise RunFailed(f"Run {run_id} {run.status}", run)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Run {run_id} still {run.status} after {timeout}s")
            time.sleep(interval)
            interval = min(interval * 2, max_interval)


class Scheduler:
    """The scheduler role's status (admin).

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def status(self) -> SchedulerStatus:
        """Report the holder and the queue lengths.

        Returns
        -------
        SchedulerStatus
            Holder, heartbeat and counts.
        """
        data = self._transport.request(ROUTES["scheduler.status"]).json()
        return SchedulerStatus(
            holder=data["holder"],
            heartbeat_at=_when(data["heartbeat_at"]),
            active_runs=data["active_runs"],
            pending=data["pending"],
            ready=data["ready"],
            running=data["running"],
        )


class Workers:
    """The workers of this instance (admin).

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(self) -> _WorkerList:
        """List the registered workers, by id.

        A worker is `alive` while its heartbeat is younger than the server's lease and `lost`
        after that; a lost worker's row is kept for an hour. Each entry shows the run ids of
        what the worker is doing (a query, a deploy, a pipeline task), never SQL, and `takes`
        says which work it takes: `all`, `interactive` or `pipeline` (`all` when the server is
        of an earlier release and sends none).

        Returns
        -------
        list of WorkerInfo
            The workers; empty when none is registered.

        Raises
        ------
        lakana.errors.PermissionDenied
            Without `MANAGE_ENVIRONMENT` on the instance (as `scheduler.status`).
        """
        return [
            WorkerInfo(
                id=item["id"],
                host=item["host"],
                version=item["version"],
                started_at=datetime.fromisoformat(item["started_at"]),
                heartbeat_at=datetime.fromisoformat(item["heartbeat_at"]),
                status=item["status"],
                takes=item["takes"] if isinstance(item.get("takes"), str) else "all",
                query_run_id=item["query_run_id"],
                deploy_run_id=item["deploy_run_id"],
                task_run_id=item["task_run_id"],
            )
            for item in self._transport.request(ROUTES["workers.list"]).json()
        ]
