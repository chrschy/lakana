"""`RunService.list_runs_page`: filters and cursor of the runs list."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from sqlalchemy.orm import Session
from test_projects_dag import make_asset
from test_projects_deploys import _deployed, _repo

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.models import JobVersion, Run, RunStatus, RunTrigger
from lakana_core.pagination import encode_cursor
from lakana_core.projects.run_policy import RunCaps
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService

_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def version(session: Session, tmp_path: Path, principal: PrincipalRef) -> JobVersion:
    """Deploy `analytics` with one asset to `dev`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The admin.

    Returns
    -------
    JobVersion
        The current version.
    """
    _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    return _deployed(session, principal, _repo(session), "a" * 40, make_asset("gold.daily", ()))


def _service(session: Session) -> RunService:
    """Build a run service that allows everything.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    RunService
        The service.
    """
    return RunService(
        session, AllowAllAuthorization(), caps=RunCaps(max_retries=5, max_timeout_seconds=600)
    )


def _make_run(
    session: Session,
    principal: PrincipalRef,
    *,
    minutes: int = 0,
    status: RunStatus = RunStatus.SUCCEEDED,
    trigger: RunTrigger | None = None,
    job: str | None = None,
) -> Run:
    """Start a run of `analytics` and set the fields the list filters on.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The caller.
    minutes
        Minutes after `_T0` it was created.
    status
        Its status (a finished one by default, so the next start is allowed).
    trigger
        What started it.
    job
        The job it belongs to.

    Returns
    -------
    Run
        The row.
    """
    started = _service(session).start(principal, "dev", "analytics", Selection())
    run = session.get(Run, started.id)
    assert run is not None
    run.created_at = _T0 + timedelta(minutes=minutes)
    run.status = status
    run.trigger = trigger
    run.job = job
    session.commit()
    return run


def _ids(page: tuple[list, str | None]) -> list[uuid.UUID]:
    """Return the ids of a page's runs.

    Parameters
    ----------
    page
        What `list_runs_page` returned.

    Returns
    -------
    list of uuid.UUID
        The ids in order.
    """
    return [v.id for v in page[0]]


def test_status_filters_one_or_several_values(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    ok = _make_run(session, principal, minutes=1)
    bad = _make_run(session, principal, minutes=2, status=RunStatus.FAILED)
    gone = _make_run(session, principal, minutes=3, status=RunStatus.CANCELLED)
    service = _service(session)
    assert _ids(service.list_runs_page(principal, "dev", status=["failed"])) == [bad.id]
    both = service.list_runs_page(principal, "dev", status=["failed", "cancelled"])
    assert _ids(both) == [gone.id, bad.id]
    assert _ids(service.list_runs_page(principal, "dev", status=["succeeded"])) == [ok.id]


def test_trigger_job_and_since_filter(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    old = _make_run(session, principal, minutes=1, trigger=RunTrigger.SCHEDULE, job="nightly")
    new = _make_run(session, principal, minutes=60, trigger=RunTrigger.API, job="hourly")
    adhoc = _make_run(session, principal, minutes=120)
    service = _service(session)
    assert _ids(service.list_runs_page(principal, "dev", trigger=["schedule"])) == [old.id]
    assert _ids(service.list_runs_page(principal, "dev", trigger=["api", "schedule"])) == [
        new.id,
        old.id,
    ]
    assert _ids(service.list_runs_page(principal, "dev", job="hourly")) == [new.id]
    since = _T0 + timedelta(minutes=60)
    assert _ids(service.list_runs_page(principal, "dev", since=since)) == [adhoc.id, new.id]
    assert _ids(service.list_runs_page(principal, "dev", since=since + timedelta(seconds=1))) == [
        adhoc.id
    ]


def test_filters_combine_with_and(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    _make_run(session, principal, minutes=1, status=RunStatus.FAILED, job="a")
    hit = _make_run(
        session, principal, minutes=2, status=RunStatus.FAILED, job="a", trigger=RunTrigger.MANUAL
    )
    _make_run(session, principal, minutes=3, status=RunStatus.SUCCEEDED, job="a")
    page = _service(session).list_runs_page(
        principal,
        "dev",
        repository="analytics",
        status=["failed"],
        job="a",
        trigger=["manual"],
        since=_T0,
    )
    assert _ids(page) == [hit.id]
    assert page[1] is None


def test_an_unknown_status_or_trigger_is_refused_with_the_allowed_values(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    service = _service(session)
    with pytest.raises(ValidationError, match="`status` must be one of queued, running"):
        service.list_runs_page(principal, "dev", status=["done"])
    with pytest.raises(ValidationError, match="`trigger` must be one of schedule, api, manual"):
        service.list_runs_page(principal, "dev", trigger=["cron"])


def test_since_without_a_time_zone_is_refused(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    with pytest.raises(ValidationError, match="`since` has no time zone"):
        _service(session).list_runs_page(principal, "dev", since=datetime(2026, 10, 1, 12, 0))


def test_a_limit_below_one_is_refused(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    with pytest.raises(ValidationError, match="limit must be at least 1"):
        _service(session).list_runs_page(principal, "dev", limit=0)


def test_an_unknown_repository_is_not_found(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    with pytest.raises(NotFoundError):
        _service(session).list_runs_page(principal, "dev", repository="ghost")


def test_pages_continue_strictly_after_the_cursor_across_equal_times(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    runs = [_make_run(session, principal, minutes=5) for _ in range(5)]
    older = _make_run(session, principal, minutes=1)
    newer = _make_run(session, principal, minutes=9)
    expected = [newer.id, *sorted((r.id for r in runs), reverse=True), older.id]
    service = _service(session)
    seen: list[uuid.UUID] = []
    cursor = None
    pages = 0
    while True:
        page, cursor = service.list_runs_page(principal, "dev", limit=2, cursor=cursor)
        pages += 1
        seen += [v.id for v in page]
        if cursor is None:
            break
    assert (pages, seen) == (4, expected)


def test_a_new_run_while_paging_neither_repeats_nor_skips(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    first = [_make_run(session, principal, minutes=m) for m in (1, 2, 3, 4)]
    service = _service(session)
    page, cursor = service.list_runs_page(principal, "dev", limit=2)
    _make_run(session, principal, minutes=50)
    rest, last = service.list_runs_page(principal, "dev", limit=2, cursor=cursor)
    assert last is None
    assert [v.id for v in page + rest] == [r.id for r in reversed(first)]


def test_the_cursor_keeps_the_filter_of_the_caller_not_its_own(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    for m in (1, 2, 3):
        _make_run(session, principal, minutes=m, status=RunStatus.FAILED)
    _make_run(session, principal, minutes=4)
    service = _service(session)
    first, cursor = service.list_runs_page(principal, "dev", status=["failed"], limit=2)
    second, last = service.list_runs_page(
        principal, "dev", status=["failed"], limit=2, cursor=cursor
    )
    assert (len(first), len(second), last) == (2, 1, None)


def test_a_cursor_of_another_list_is_refused(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    other = encode_cursor("deploys", _T0, uuid.uuid4())
    with pytest.raises(ValidationError, match="came from the `deploys` list"):
        _service(session).list_runs_page(principal, "dev", cursor=other)


def test_list_runs_is_the_first_page_without_a_cursor(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    runs = [_make_run(session, principal, minutes=m) for m in (1, 2, 3)]
    assert [v.id for v in _service(session).list_runs(principal, "dev", limit=2)] == [
        runs[2].id,
        runs[1].id,
    ]
