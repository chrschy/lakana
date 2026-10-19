"""The SDK's filters and cursors on the runs, queries and deploys lists."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, csv_param
from lakana.resources.deploys import Deploys
from lakana.resources.queries import Queries
from lakana.resources.runs import Runs

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_RUN: dict[str, Any] = {
    "id": "00000000-0000-0000-0000-0000000000cc",
    "environment": "dev",
    "repository": "analytics",
    "project_version_id": "00000000-0000-0000-0000-0000000000bb",
    "commit_sha": "a" * 40,
    "kind": "full",
    "status": "failed",
    "cancel_requested": False,
    "triggered_by": "admin",
    "run_as": "admin",
    "resumed_from_run_id": None,
    "selection": {"patterns": [], "upstream": False, "downstream": False},
    "error_message": None,
    "created_at": "2026-10-14T12:00:00+00:00",
    "started_at": None,
    "finished_at": None,
    "tasks": [],
}
_ENTRY: dict[str, Any] = {
    "id": "00000000-0000-0000-0000-0000000000dd",
    "submitter": "alice",
    "kind": "read",
    "target": None,
    "status": "QUEUED",
    "created_at": "2026-10-14T12:00:00+00:00",
    "started_at": None,
    "finished_at": None,
    "error_cause": None,
    "waiting_for": None,
}
_DEPLOY: dict[str, Any] = {
    "id": "00000000-0000-0000-0000-0000000000ee",
    "environment": "dev",
    "repository": "analytics",
    "kind": "deploy",
    "status": "succeeded",
    "requested_ref": "main",
    "resolved_sha": "a" * 40,
    "rollback_to_project_version_id": None,
    "error_message": None,
    "diff": None,
    "project_version_id": None,
    "principal_id": None,
    "created_at": "2026-10-14T12:00:00+00:00",
    "started_at": None,
    "finished_at": None,
}


def _transport(
    pages: list[tuple[list[dict[str, Any]], str | None]], seen: list[httpx.Request]
) -> HttpTransport:
    """Build a transport that answers successive requests with successive pages.

    Parameters
    ----------
    pages
        The body and the `X-Next-Cursor` header (or `None`) of each answer; the last repeats.
    seen
        Where to record the requests.

    Returns
    -------
    HttpTransport
        The transport.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body, cursor = pages[min(len(seen), len(pages) - 1)]
        seen.append(request)
        headers = {} if cursor is None else {NEXT_CURSOR_HEADER: cursor}
        return httpx.Response(200, json=body, headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return HttpTransport(_CONFIG, http_client=client)


@pytest.mark.parametrize(
    ("values", "joined"),
    [
        (None, None),
        ("failed", "failed"),
        (["failed", "cancelled"], "failed,cancelled"),
        ([], None),
        ("", None),
    ],
)
def test_csv_param_joins_the_values_of_a_filter(
    values: str | list[str] | None, joined: str | None
) -> None:
    assert csv_param(values) == joined


def test_runs_list_sends_every_filter_and_returns_the_runs() -> None:
    seen: list[httpx.Request] = []
    runs = Runs(_transport([([_RUN], "next")], seen))
    since = datetime(2026, 10, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    listed = runs.list(
        "dev",
        repository="analytics",
        status=["failed", "cancelled"],
        trigger="schedule",
        job="nightly",
        since=since,
        limit=5,
        cursor="abc",
    )
    assert [r.id for r in listed] == [_RUN["id"]]
    params = seen[0].url.params
    assert dict(params) == {
        "repository": "analytics",
        "status": "failed,cancelled",
        "trigger": "schedule",
        "job": "nightly",
        "since": "2026-10-01T12:00:00+02:00",
        "limit": "5",
        "cursor": "abc",
    }


def test_runs_list_page_returns_the_cursor_of_the_header() -> None:
    seen: list[httpx.Request] = []
    page = Runs(_transport([([_RUN], "next-one")], seen)).list_page("dev")
    assert (page.next_cursor, len(page.items)) == ("next-one", 1)
    assert dict(seen[0].url.params) == {}
    last = Runs(_transport([([_RUN], None)], [])).list_page("dev")
    assert last.next_cursor is None


def test_runs_iter_follows_the_cursor_until_the_last_page() -> None:
    seen: list[httpx.Request] = []
    runs = Runs(_transport([([_RUN], "c1"), ([_RUN], "c2"), ([_RUN], None)], seen))
    assert len(list(runs.iter("dev", status="failed", page_size=1))) == 3
    assert [r.url.params.get("cursor") for r in seen] == [None, "c1", "c2"]
    assert all(r.url.params["status"] == "failed" for r in seen)
    assert all(r.url.params["limit"] == "1" for r in seen)


def test_queries_list_sends_the_filters_and_page_returns_the_cursor() -> None:
    seen: list[httpx.Request] = []
    queries = Queries(_transport([([_ENTRY], "q-next")], seen))
    entries = queries.list("dev", status="failed", submitter="bob", limit=7, cursor="q0")
    assert [e.id for e in entries] == [_ENTRY["id"]]
    assert dict(seen[0].url.params) == {
        "include_ended": "false",
        "status": "failed",
        "submitter": "bob",
        "limit": "7",
        "cursor": "q0",
    }
    page = queries.list_page("dev", include_ended=True)
    assert (page.next_cursor, seen[1].url.params["include_ended"]) == ("q-next", "true")
    assert "status" not in seen[1].url.params


def test_deploys_list_sends_the_filter_and_page_returns_the_cursor() -> None:
    seen: list[httpx.Request] = []
    deploys = Deploys(_transport([([_DEPLOY], "d-next")], seen))
    listed = deploys.list("dev", repository="analytics", limit=3, cursor="d0")
    assert [d.id for d in listed] == [_DEPLOY["id"]]
    assert dict(seen[0].url.params) == {"repository": "analytics", "limit": "3", "cursor": "d0"}
    page = deploys.list_page("dev")
    assert (page.next_cursor, dict(seen[1].url.params)) == ("d-next", {})
