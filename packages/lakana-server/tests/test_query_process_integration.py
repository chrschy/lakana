"""The exit test: queries in their own process, on Postgres and SeaweedFS.

A real HTTP server (uvicorn on a loopback port) on real Postgres and SeaweedFS, and a worker
that runs in a thread of the test process: `poll_once` claims a query run and `run_one` starts a
real query child (`lakana_worker.query_child`) for it, exactly as a worker process does. What
the unit tier cannot show is here: a write whose child is killed mid-stream commits nothing and
leaves no object, a cancel reaches a running query through the REST route, a cancel racing the
dequeue on Postgres ends in one answer, and the cancel route's authorization under real grants.

Every wait has a bound. The one long query is `_LONG_SQL`: it runs about nine seconds by
itself, so a cancel that did not work still ends the test, and it holds almost no memory.
"""

from __future__ import annotations

import os
import signal
import threading
import time
import uuid
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager

import pyarrow as pa
import pytest
from sqlalchemy.orm import Session

import lakana_worker.pipeline as pipeline
import lakana_worker.poller
from lakana._models import QueryStatus
from lakana.errors import Conflict, NotFound
from lakana.resources.queries import QueryRun as SdkRun
from lakana_core.auth import AuthorizationService
from lakana_core.config import QueryLimitsConfig
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import (
    AuditEventType,
)
from lakana_core.secrets.keys import KeyProvider
from lakana_worker.executor import KILLED_FROM_OUTSIDE_MESSAGE
from lakana_worker.poller import poll_once
from lakana_worker.query_process import REGISTRY, QueryChild
from tests.integration_helpers import backend_pid, unique_name, wait_for_lock_waiter
from tests.python_asset_helpers import (
    QueryStack,
    bound_attempt,
    long_sql,
    query_stack,
    until,
)

pytestmark = pytest.mark.integration

_RUNNING_SECONDS = 10.0
_FINAL_SECONDS = 15.0
_JOIN_SECONDS = 60.0


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[QueryStack]:
    """Start Postgres, SeaweedFS and a real HTTP server; no worker runs until a test starts one.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    QueryStack
        The stack.
    """
    with query_stack(tmp_path_factory) as built:
        yield built


@contextmanager
def _worker(stack: QueryStack) -> Generator[threading.Thread, None, None]:
    """Run a query worker in a thread, as a task-less worker process would.

    Parameters
    ----------
    stack
        The stack.

    Yields
    ------
    threading.Thread
        The worker's thread, alive.

    Raises
    ------
    BaseException
        Whatever ended the thread, re-raised after it was joined.
    AssertionError
        If the thread does not end within a minute of being asked to.
    """
    stop = threading.Event()
    errors: list[BaseException] = []

    def loop() -> None:
        """Claim and run queued queries until stopped, recording whatever escapes."""
        try:
            while not stop.is_set():
                with Session(stack.db) as session:
                    busy = poll_once(session, GrantAuthorization(session), only_queries=True)
                if not busy:
                    stop.wait(0.05)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=loop, name="lakana-test-queries", daemon=True)
    thread.start()
    try:
        yield thread
    finally:
        stop.set()
        thread.join(timeout=_JOIN_SECONDS)
    assert not thread.is_alive(), "the worker thread did not end"
    if errors:
        raise errors[0]


def _cancel_leftovers(stack: QueryStack, environment: str, *runs: SdkRun) -> None:
    """Cancel the runs a test left queued, so no later worker picks them up.

    Parameters
    ----------
    stack
        The stack.
    environment
        The environment.
    *runs
        The runs.
    """
    for run in runs:
        stack.admin.queries.cancel(run.id, environment)


def test_a_read_runs_in_a_child_and_returns_its_rows(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read starts exactly one child, returns its rows, and leaves no child registered."""
    environment = stack.environment()
    started: list[int | None] = []
    real_add = REGISTRY.add

    def counting_add(child: QueryChild) -> None:
        """Count the children that are registered, then register."""
        started.append(child.pid)
        real_add(child)

    monkeypatch.setattr(REGISTRY, "add", counting_add)
    run = stack.admin.queries.submit("SELECT id FROM bronze.nums ORDER BY id", environment)
    with _worker(stack):
        run.wait(timeout=60)
    assert run.result().column("id").to_pylist() == [1, 2, 3]
    assert len(started) == 1
    assert REGISTRY.pids() == []


def test_a_write_commits_after_a_complete_stream(stack: QueryStack) -> None:
    """`--mode create` from a `SELECT`: the table exists with the `SELECT`'s rows, in a snapshot."""
    environment = stack.environment()
    run = stack.admin.queries.submit(
        "SELECT id * 10 AS id FROM bronze.nums", environment, into="gold.tens", mode="create"
    )
    with _worker(stack):
        status = run.wait(timeout=60)
        rows = stack.admin.sql("SELECT id FROM gold.tens ORDER BY id", environment, timeout=60)
    assert (status.status, status.rows_written) == ("SUCCEEDED", 3)
    assert status.snapshot_id is not None
    assert rows.column("id").to_pylist() == [10, 20, 30]
    assert stack.row(run.id).snapshot_id == status.snapshot_id


_REAL_STREAM_QUERY = pipeline.stream_query
"""The real function, taken before a test replaces the name `pipeline.py` calls."""


@contextmanager
def _kill_after_first_batch(
    sql: str,
    parameters: Mapping[str, object] | None = None,
    *,
    read_prefixes: Sequence[str] = (),
    spill: bool = False,
    limits: QueryLimitsConfig | None = None,
) -> Generator[tuple[pa.Schema, Iterator[pa.RecordBatch]], None, None]:
    """Run the real `stream_query`, and end the child from outside once one batch was written.

    Parameters
    ----------
    sql
        The rewritten SQL.
    parameters
        Its bound values.
    read_prefixes
        The locations the query process may open.
    spill
        Whether the query process may spill.
    limits
        The query limits.

    Yields
    ------
    tuple
        The schema, and the batches with the kill inserted after the first.
    """
    with _REAL_STREAM_QUERY(
        sql, parameters, read_prefixes=read_prefixes, spill=spill, limits=limits
    ) as (schema, batches):

        def cut() -> Iterator[pa.RecordBatch]:
            """Hand over every batch and, after the first, kill the child's process group.

            Yields
            ------
            pa.RecordBatch
                Each batch.
            """
            killed = False
            for batch in batches:
                yield batch
                if not killed:
                    killed = True
                    for pid in REGISTRY.pids():
                        os.killpg(pid, signal.SIGKILL)

        yield schema, cut()


def test_a_write_whose_child_is_killed_mid_stream_commits_nothing(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child killed after the first batch: the run fails, no table, no object, no child."""
    environment = stack.environment()
    monkeypatch.setattr(pipeline, "stream_query", _kill_after_first_batch)
    before = stack.storage_listing(environment)
    # Eight batches: the child still waits on its pipe when the first is consumed.
    run = stack.admin.queries.submit(
        "SELECT i AS id FROM range(500000) AS t(i)", environment, into="gold.cut", mode="create"
    )
    with _worker(stack):
        until(lambda: run.status().status, {"FAILED"}, _FINAL_SECONDS)
    status = run.status()
    assert status.error_cause == "internal"
    assert "ended from outside the worker (signal 9)" in (status.error_message or "")
    assert (status.error_message or "").startswith(
        KILLED_FROM_OUTSIDE_MESSAGE.format(signal=9).split("(signal")[0]
    )
    assert status.snapshot_id is None
    with pytest.raises(NotFound):
        stack.admin.tables.get(environment, "gold.cut")
    assert stack.storage_listing(environment) == before
    assert REGISTRY.pids() == []


def test_a_running_query_is_cancelled_through_the_api(stack: QueryStack) -> None:
    """Cancel while it runs: `CANCELLED` within the bound, no result, two audit events."""
    environment = stack.environment()
    run = stack.admin.queries.submit(long_sql(), environment)
    with _worker(stack) as thread:
        until(lambda: run.status().status, {"RUNNING"}, _RUNNING_SECONDS)
        asked = time.monotonic()
        answer = run.cancel()
        assert answer.status in {"RUNNING", "CANCELLED"}
        until(lambda: run.status().status, {"CANCELLED", "SUCCEEDED", "FAILED"}, _FINAL_SECONDS)
        latency = time.monotonic() - asked
    assert not thread.is_alive()
    status = run.status()
    assert (status.status, status.error_cause) == ("CANCELLED", "cancelled")
    assert latency < _FINAL_SECONDS
    with pytest.raises(Conflict):
        run.result()
    assert stack.row(run.id).result_location is None
    admin_id = stack.principal_id("admin")
    cancelled = stack.events(environment, AuditEventType.QUERY_CANCELLED)
    assert [(e.principal_id, e.detail["status"]) for e in cancelled] == [(admin_id, "RUNNING")]
    completed = stack.events(environment, AuditEventType.QUERY_COMPLETED)
    assert [e.detail["status"] for e in completed] == ["CANCELLED"]
    assert REGISTRY.pids() == []


def test_a_cancelled_write_leaves_the_table_as_it_was(stack: QueryStack) -> None:
    """A `replace` cancelled while its query runs: same snapshot, no new object, run `CANCELLED`."""
    environment = stack.environment()
    first = stack.admin.queries.submit(
        "SELECT id FROM bronze.nums", environment, into="gold.keep", mode="create"
    )
    with _worker(stack):
        first.wait(timeout=60)
    snapshot = stack.admin.tables.get(environment, "gold.keep").snapshot_id
    assert snapshot is not None
    before = stack.storage_listing(environment)
    replace = stack.admin.queries.submit(
        "SELECT sum(hash(a.range + b.range))::BIGINT AS id FROM range(40000) a, range(40000) b",
        environment,
        into="gold.keep",
        mode="replace",
    )
    with _worker(stack):
        until(lambda: replace.status().status, {"RUNNING"}, _RUNNING_SECONDS)
        replace.cancel()
        until(lambda: replace.status().status, {"CANCELLED", "SUCCEEDED", "FAILED"}, _FINAL_SECONDS)
    assert replace.status().status == "CANCELLED"
    assert stack.storage_listing(environment) == before
    assert stack.admin.tables.get(environment, "gold.keep").snapshot_id == snapshot
    with _worker(stack):
        rows = stack.admin.sql("SELECT id FROM gold.keep ORDER BY id", environment, timeout=60)
    assert rows.column("id").to_pylist() == [1, 2, 3]


def test_a_queued_query_is_cancelled_at_once_and_a_worker_passes_it_by(stack: QueryStack) -> None:
    """Cancel before any worker runs: `CANCELLED` now, and one poll finds nothing to claim."""
    environment = stack.environment()
    run = stack.admin.queries.submit("SELECT id FROM bronze.nums", environment)
    answer = run.cancel()
    assert (answer.status, answer.error_cause) == ("CANCELLED", "cancelled")
    with Session(stack.db) as session:
        assert poll_once(session, GrantAuthorization(session), only_queries=True) is False
    row = stack.row(run.id)
    assert (row.status.value, row.started_at) == ("CANCELLED", None)
    assert run.status().status == "CANCELLED"
    assert stack.events(environment, AuditEventType.QUERY_COMPLETED) == []
    cancelled = stack.events(environment, AuditEventType.QUERY_CANCELLED)
    assert [e.detail["status"] for e in cancelled] == ["QUEUED"]


def test_a_worker_that_polls_skips_a_query_cancelled_while_it_was_queued(stack: QueryStack) -> None:
    """Behind a running query: a queued one is cancelled, never started; the next one runs."""
    environment = stack.environment()
    running = stack.admin.queries.submit(long_sql("a"), environment)
    with _worker(stack):
        until(lambda: running.status().status, {"RUNNING"}, _RUNNING_SECONDS)
        skipped = stack.admin.queries.submit(long_sql("b"), environment)
        after = stack.admin.queries.submit("SELECT id FROM bronze.nums ORDER BY id", environment)
        assert skipped.cancel().status == "CANCELLED"
        running.cancel()
        until(lambda: running.status().status, {"CANCELLED", "SUCCEEDED"}, _FINAL_SECONDS)
        after.wait(timeout=_FINAL_SECONDS)
    row = stack.row(skipped.id)
    assert (row.status.value, row.started_at) == ("CANCELLED", None)
    assert after.result().column("id").to_pylist() == [1, 2, 3]
    texts = [
        e.detail["sql_text"] for e in stack.events(environment, AuditEventType.QUERY_COMPLETED)
    ]
    assert not any(" AS b " in text for text in texts)
    assert len(texts) == 2


def test_a_cancel_that_meets_the_dequeue_lock_ends_the_run_as_cancelled(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker holds the row it claimed; a cancel waits for it, then asks the running query."""
    environment = stack.environment()
    run = stack.admin.queries.submit(long_sql(), environment)
    real = lakana_worker.poller.run_one
    answers: list[QueryStatus] = []
    failures: list[BaseException] = []

    def cancel_now() -> None:
        """Send the cancel; it blocks on the claimed row until the worker commits."""
        try:
            answers.append(run.cancel())
        except BaseException as error:
            failures.append(error)

    cancel = threading.Thread(target=cancel_now, name="lakana-test-cancel", daemon=True)

    def holding_the_claim(
        session: Session,
        query_run_id: uuid.UUID,
        authz: AuthorizationService,
        key_provider: KeyProvider | None = None,
        limits: QueryLimitsConfig | None = None,
        **kwargs: object,
    ) -> object:
        """Start the cancel while this session holds the claimed row, wait until it waits.

        Returns
        -------
        object
            What the real `run_one` returns.
        """
        holder = backend_pid(session)
        cancel.start()
        wait_for_lock_waiter(stack.db, holder, timeout=10)
        return real(session, query_run_id, authz, key_provider, limits=limits, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(lakana_worker.poller, "run_one", holding_the_claim)
    with _worker(stack):
        until(lambda: run.status().status, {"CANCELLED", "SUCCEEDED", "FAILED"}, _FINAL_SECONDS)
    cancel.join(timeout=_JOIN_SECONDS)
    assert not cancel.is_alive()
    assert failures == []
    assert answers[0].status in {"RUNNING", "CANCELLED"}
    status = run.status()
    assert (status.status, status.error_cause) == ("CANCELLED", "cancelled")
    assert stack.row(run.id).result_location is None
    cancelled = stack.events(environment, AuditEventType.QUERY_CANCELLED)
    assert [e.detail["status"] for e in cancelled] == ["RUNNING"]
    completed = stack.events(environment, AuditEventType.QUERY_COMPLETED)
    assert [e.detail["status"] for e in completed] == ["CANCELLED"]


def _principal(stack: QueryStack, **grants: dict[str, str]) -> tuple[str, str]:
    """Create a user with grants and a personal token.

    Parameters
    ----------
    stack
        The stack.
    **grants
        Keyword names are labels only; each value is the keyword arguments of one
        `grants.grant` call (`action` or `role`, `kind`, `environment`).

    Returns
    -------
    tuple of str
        The user's name and its token.
    """
    name = unique_name()
    stack.admin.principals.create(name, "USER")
    for grant in grants.values():
        stack.admin.grants.grant(principal=name, **grant)
    return name, stack.admin.tokens.issue(name, "pat").token


def test_another_user_cannot_cancel_but_an_environment_manager_can(stack: QueryStack) -> None:
    """404 (the status route's own body) for `SUBMIT_QUERY` only; 200 with `MANAGE_ENVIRONMENT`."""
    environment = stack.environment()
    other = stack.environment()
    on = {"kind": "ENVIRONMENT", "environment": environment}
    submit = {"action": "SUBMIT_QUERY", **on}
    manage = {"action": "MANAGE_ENVIRONMENT", **on}
    plain, plain_token = _principal(stack, submit=submit)
    manager, manager_token = _principal(stack, manage=manage)
    elsewhere, elsewhere_token = _principal(
        stack,
        submit=submit,
        manage={"action": "MANAGE_ENVIRONMENT", "kind": "ENVIRONMENT", "environment": other},
    )
    group = unique_name()
    member, member_token = _principal(stack)
    stack.admin.groups.create(group)
    stack.admin.groups.add_member(group, member)
    stack.admin.grants.grant(group=group, **manage)
    boss, boss_token = _principal(stack, admin={"role": "ADMIN", "kind": "INSTANCE"})
    runs = [stack.admin.queries.submit("SELECT id FROM bronze.nums", environment) for _ in range(5)]
    refused, by_manager, by_group, by_instance_admin, kept = runs
    try:
        with stack.client(plain_token) as client:
            with pytest.raises(NotFound) as read:
                client.queries.get(refused.id, environment).status()
            with pytest.raises(NotFound) as cancel:
                client.queries.cancel(refused.id, environment)
            assert str(cancel.value) == str(read.value)
        with stack.client(elsewhere_token) as client, pytest.raises(NotFound):
            client.queries.cancel(refused.id, environment)
        assert refused.status().status == "QUEUED"
        for run, token in (
            (by_manager, manager_token),
            (by_group, member_token),
            (by_instance_admin, boss_token),
        ):
            with stack.client(token) as client:
                assert client.queries.cancel(run.id, environment).status == "CANCELLED"
            assert run.status().status == "CANCELLED"
        assert kept.status().status == "QUEUED"
        denied = [
            e
            for e in stack.events(environment, AuditEventType.AUTHORIZATION_DENIED)
            if e.detail.get("reason") == "not_submitter"
        ]
        assert {e.principal_id for e in denied} == {
            stack.principal_id(plain),
            stack.principal_id(elsewhere),
        }
        assert {e.detail["action"] for e in denied} == {"MANAGE_ENVIRONMENT"}
        cancelled = stack.events(environment, AuditEventType.QUERY_CANCELLED)
        assert {e.principal_id for e in cancelled} == {
            stack.principal_id(manager),
            stack.principal_id(member),
            stack.principal_id(boss),
        }
    finally:
        _cancel_leftovers(stack, environment, refused, kept)


def test_a_task_token_cancels_only_its_own_query(stack: QueryStack) -> None:
    """A task token cancels a query of that token; another principal's query is a 404."""
    environment = stack.environment()
    attempt = bound_attempt(
        stack.db, stack.admin, environment, asset="gold.scores", inputs={"obs": "bronze.nums"}
    )
    foreign = stack.admin.queries.submit("SELECT id FROM bronze.nums", environment)
    try:
        with stack.client(attempt.token) as task:
            own = task.queries.submit("SELECT id FROM bronze.nums", environment)
            with pytest.raises(NotFound):
                task.queries.cancel(foreign.id, environment)
            assert own.cancel().status == "CANCELLED"
        assert foreign.status().status == "QUEUED"
    finally:
        _cancel_leftovers(stack, environment, foreign)
