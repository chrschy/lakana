"""Jobs of the platform API: list them, start one, pause and resume its schedule.

The server records and reads; the scheduler of the worker fires the schedules. A start by hand
or through the API goes through the same core code as `POST .../runs`, with the same permission
(`RUN`), but a busy project makes it wait (202) instead of refusing it (409).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from lakana_core.auth import PrincipalRef
from lakana_core.jobs.service import JobService, JobView, StartResult, WaitingStart
from lakana_core.notifications.views import DeliveryView
from lakana_server.api.platform.runs import run_out
from lakana_server.api.platform.schemas import (
    DeliveryOut,
    JobOut,
    JobRunRequest,
    JobStartOut,
    NotifyTestOut,
    OnDemandTriggerOut,
    ScheduleTriggerOut,
    WaitingStartOut,
)
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_job_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/environments/{env}", tags=["jobs"]
)

_JOB = Annotated[str, Path(min_length=1, max_length=255)]


def _job_out(view: JobView) -> JobOut:
    """Build a job body, with the reason as `WaitReason.to_json` writes it.

    Parameters
    ----------
    view
        The job.

    Returns
    -------
    JobOut
        The body.
    """
    fields = dataclasses.asdict(view)
    fields["select"] = list(view.select)
    fields["waiting_for"] = None if view.waiting_for is None else view.waiting_for.to_json()
    triggers: list[ScheduleTriggerOut | OnDemandTriggerOut] = []
    if view.cron is not None and view.timezone is not None:
        triggers.append(
            ScheduleTriggerOut(
                cron=view.cron,
                timezone=view.timezone,
                paused=view.paused,
                next_run_at=None if view.paused else view.next_run_at,
            )
        )
    triggers.append(OnDemandTriggerOut())
    return JobOut(**fields, triggers=triggers)


def _waiting_out(waiting: WaitingStart) -> WaitingStartOut:
    """Build a waiting-start body.

    Parameters
    ----------
    waiting
        The waiting start.

    Returns
    -------
    WaitingStartOut
        The body, with its sentence.
    """
    return WaitingStartOut(**dataclasses.asdict(waiting), message=waiting.message)


def _start_out(result: StartResult) -> JobStartOut:
    """Build a start body.

    Parameters
    ----------
    result
        What the start did.

    Returns
    -------
    JobStartOut
        The run, or the waiting start.
    """
    return JobStartOut(
        run=None if result.run is None else run_out(result.run),
        waiting=None if result.waiting is None else _waiting_out(result.waiting),
    )


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    env: str,
    repository: str | None = Query(default=None, max_length=255),
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> list[JobOut]:
    """List the jobs of the deployed repositories (needs `READ_ENVIRONMENT`).

    Parameters
    ----------
    env
        Environment name.
    repository
        Only this repository's jobs.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    list of JobOut
        By repository, then job name.
    """
    return [_job_out(view) for view in service.list(principal, env, repository)]


@router.get("/jobs/{job}", response_model=JobOut)
def get_job(
    env: str,
    job: _JOB,
    repository: str | None = Query(default=None, max_length=255),
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> JobOut:
    """Show one job, with its triggers (needs `READ_ENVIRONMENT`).

    Parameters
    ----------
    env
        Environment name.
    job
        The job's name.
    repository
        The repository; needed only when several repositories declare the job.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    JobOut
        The job, as the list shows it.
    """
    return _job_out(service.get(principal, env, job, repository))


@router.post(
    "/jobs/{job}/runs",
    response_model=JobStartOut,
    status_code=201,
    responses={202: {"model": JobStartOut, "description": "The project is busy: the start waits."}},
)
def start_job(
    env: str,
    job: _JOB,
    body: JobRunRequest,
    response: Response,
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> JobStartOut:
    """Start a job, or let the start wait for the active run (needs `RUN`).

    Answers 201 with the queued run, or 202 with the waiting start and the run it waits for. A
    paused job can be started.

    Parameters
    ----------
    env
        Environment name.
    job
        The job's name.
    body
        The repository.
    response
        The response, to set 202.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    JobStartOut
        The run or the waiting start.
    """
    result = service.start(principal, env, body.repository, job)
    if result.waiting is not None:
        response.status_code = 202
    return _start_out(result)


@router.post("/jobs/{job}/pause", response_model=JobOut)
def pause_job(
    env: str,
    job: _JOB,
    body: JobRunRequest,
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> JobOut:
    """Pause a job's schedule (needs `DEPLOY` on the environment).

    Parameters
    ----------
    env
        Environment name.
    job
        The job's name.
    body
        The repository.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    JobOut
        The job.
    """
    return _job_out(service.pause(principal, env, body.repository, job))


@router.post("/jobs/{job}/resume", response_model=JobOut)
def resume_job(
    env: str,
    job: _JOB,
    body: JobRunRequest,
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> JobOut:
    """Resume a job's schedule from now (needs `DEPLOY` on the environment).

    Parameters
    ----------
    env
        Environment name.
    job
        The job's name.
    body
        The repository.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    JobOut
        The job.
    """
    return _job_out(service.resume(principal, env, body.repository, job))


@router.post("/jobs/{job}/notify-test", response_model=NotifyTestOut, status_code=202)
def notify_test(
    env: str,
    job: _JOB,
    body: JobRunRequest,
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> NotifyTestOut:
    """Queue a test message to a job's webhook (needs `DEPLOY` on the environment).

    Answers 202 with the delivery id: a worker sends it, and the delivery shows how it went.

    Parameters
    ----------
    env
        Environment name.
    job
        The job's name.
    body
        The repository.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    NotifyTestOut
        The delivery id.
    """
    return NotifyTestOut(delivery_id=service.notify_test(principal, env, body.repository, job))


@router.get("/notifications/{delivery_id}", response_model=DeliveryOut)
def get_notification(
    env: str,
    delivery_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> DeliveryOut:
    """Show one notification delivery (needs `DEPLOY`, `RUN` or `READ_ENVIRONMENT`).

    A test has no run, so the right to read a run does not reach it; whoever may test a webhook
    may read the result.

    Parameters
    ----------
    env
        Environment name.
    delivery_id
        The delivery.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    DeliveryOut
        Its state, attempts and short result.
    """
    view: DeliveryView = service.delivery(principal, env, delivery_id)
    return DeliveryOut(**vars(view))
