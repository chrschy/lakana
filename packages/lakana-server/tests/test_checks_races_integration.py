"""On Postgres: the rollback, the stamp and the check results against a second session.

SQLite takes no row locks and has one writer, so the unit tier can only show what each
statement's `WHERE` clause refuses. Here every test has a task with checks whose state the real
worker produced (a deploy from git, a run, the task's write through the query pipeline, then
the worker "dies" at its first count), and then calls the catalog and the run store on real
connections of its own: `CatalogService.roll_back`, `record_result`, `mark_written`, a
scheduler tick. No worker loop runs. A session is held at a statement by `StatementPause`
(a listener on the engine, nothing in the product), at the pointer swap by a wrapper around
`commit_metadata_pointer`, or at its commit by a wrapper around its own `Session.commit`;
the other side is then seen waiting in `pg_stat_activity` before the hold is released.
Every wait has a bound.

Python assets run through the worker's own path (`PythonRuntime`) with a scripted `Runner` in
place of `uv` and the harness process: the "process" is a function of the test that writes
through the REST API with the attempt's real task token, as the harness does.
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from importlib.metadata import version as package_version
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pyarrow as pa
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

import lakana
from lakana import runner as harness
from lakana._models import WriteResult as SdkWriteResult
from lakana_core.audit.service import AuditService
from lakana_core.auth import PrincipalRef, RunContext, Securable
from lakana_core.catalog import service as service_module
from lakana_core.catalog.metadata import read_metadata
from lakana_core.catalog.service import (
    ROLLBACK_NOT_KNOWN_REASON,
    ROLLBACK_REFUSED_REASON,
    CatalogService,
    Rollback,
    failed_snapshot_tag,
)
from lakana_core.catalog.writes import WriteResult
from lakana_core.db import db_now
from lakana_core.errors import ConflictError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    OWN_TIP_NO_SNAPSHOT,
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    CheckOutcome,
    CheckResult,
    JobVersion,
    Principal,
    PrincipalType,
    RollbackOutcome,
    Run,
    RunStatus,
    TaskAttempt,
    TaskRun,
    TaskStatus,
)
from lakana_core.projects.checks import CheckSpec, checks_of
from lakana_core.runs import workers
from lakana_core.runs.checks import (
    ROLLBACK_SENTENCES,
    RUN_CANCELLED_MESSAGE,
    SET_BACK_DONE,
    SET_BACK_LOST,
    SET_BACK_NOTHING,
    not_published_message,
    record_result,
    set_back_ending,
)
from lakana_core.runs.lockdown import SUPERSEDED_MESSAGE, ProtectedWriteGuard
from lakana_core.runs.scheduler import LOST_MESSAGE, TaskTransition
from lakana_core.runs.store import apply_transitions, scheduler_tick
from lakana_core.runs.tasks import (
    Baseline,
    FailureKind,
    LeaseState,
    Written,
    awaits_checks,
    claim_task,
    mark_written,
    read_baseline,
    read_own_tip,
    read_written,
    record_attempt,
    record_baseline,
    renew_lease,
)
from lakana_core.runs.waiting import SETTING_BACK_MESSAGE
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import checks as worker_checks
from lakana_worker import task_checks as worker_task_checks
from lakana_worker import tasks as worker_tasks
from lakana_worker.deploy import DeploySettings
from lakana_worker.poller import poll_once
from lakana_worker.python_support import PythonSupport
from lakana_worker.python_tasks import PythonRuntime
from lakana_worker.runners import PythonTask, RunOutcome, StopSignal
from lakana_worker.tasks import RunEngine, TaskSettings
from lakana_worker.venvs import IndexResolver, VenvLease, VenvSettings
from tests.integration_helpers import (
    HoldPoint,
    StatementPause,
    backend_pid,
    commit_files,
    run_in_thread,
    task_row,
    thread_result,
    unique_name,
    wait_for_lock_waiter,
)
from tests.python_asset_helpers import IDS, QueryStack, query_stack

pytestmark = pytest.mark.integration

_WAIT = 30.0
"""Seconds a held thread waits to be released before its own assertion fails."""

_LEASE = 60
"""The task lease of the worker of this module: longer than any test, so it never runs out."""

_WORKER = "checks-race-worker"
_CHECK = "SELECT id FROM silver.x WHERE id > 2"
"""Fails with one row once the asset wrote ids 1 to 3; passes on the ids 7 and 8 before it."""

_YAML = "defaults:\n  retry_delay: 0\n  retries: 1\n"
_PYPROJECT = '[project]\nname = "analytics"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'


class _Died(BaseException):
    """Raised inside the worker to make it "die" between two statements."""


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[QueryStack]:
    """Start Postgres, SeaweedFS and a real HTTP server.

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


@pytest.fixture(autouse=True)
def _no_leftovers(stack: QueryStack) -> None:
    """Cancel every active run, so that no test's worker claims a task of an earlier test.

    Parameters
    ----------
    stack
        The stack.
    """
    with Session(stack.db) as session:
        session.execute(
            update(Run)
            .where(Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
            .values(status=RunStatus.CANCELLED)
        )
        session.execute(
            update(TaskRun)
            .where(TaskRun.status.in_([TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING]))
            .values(status=TaskStatus.CANCELLED)
        )
        session.commit()


@pytest.fixture(autouse=True)
def _patient_lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the catalog's locking reads 30 seconds instead of the product's 5.

    Several tests hold a row and release it once the other side is seen waiting; on a stalled
    machine that can take longer than 5 seconds, and the test would then be about the timeout.
    The one test that is about the timeout sets a short one itself.

    Parameters
    ----------
    monkeypatch
        Sets `LOCK_TIMEOUT_SECONDS` in the catalog service for the test.
    """
    monkeypatch.setattr(service_module, "LOCK_TIMEOUT_SECONDS", 30)


def _engine(stack: QueryStack, worker: str = _WORKER) -> RunEngine:
    """Build a worker's run engine on the stack's database, its worker row registered.

    Parameters
    ----------
    stack
        The stack.
    worker
        The worker's id.

    Returns
    -------
    RunEngine
        The engine; real grants decide (`GrantAuthorization`).
    """
    with Session(stack.db) as session:
        workers.register(session, worker, host="test", version="test")
        session.commit()
    return RunEngine(
        session_factory=lambda: Session(stack.db),
        authz_factory=GrantAuthorization,
        settings=TaskSettings(worker_id=worker, lease_seconds=_LEASE, renew_seconds=20.0),
    )


def _drain(stack: QueryStack, engine: RunEngine | None = None) -> None:
    """Run queued queries, deploys and (with an engine) ready tasks in the calling thread.

    Parameters
    ----------
    stack
        The stack.
    engine
        Executes ready tasks; `None` runs only queries and deploys.
    """
    settings = DeploySettings(
        repo_roots=(str(stack.services.roots),),
        python=PythonSupport(server_url=stack.url, uv=shutil.which("uv") or "/usr/bin/uv"),
    )
    worker = None if engine is None else engine.settings.worker_id
    while True:
        with Session(stack.db) as session:
            if not poll_once(
                session,
                GrantAuthorization(session),
                deploy=settings,
                engine=engine,
                worker_id=worker,
            ):
                return


def _tick(stack: QueryStack) -> int:
    """Run one scheduler tick, as the leader would.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    int
        The transitions it applied.
    """
    with Session(stack.db) as session:
        return scheduler_tick(session, db_now(session), lambda: 0.5)


def _ids(stack: QueryStack, environment: str, table: str = "silver.x") -> list[int]:
    """Read a table's ids through a query that this thread then runs itself.

    Parameters
    ----------
    stack
        The stack.
    environment
        The environment.
    table
        The table.

    Returns
    -------
    list of int
        The ids, sorted.
    """
    run = stack.admin.queries.submit(f"SELECT id FROM {table} ORDER BY id", environment)
    _drain(stack)
    run.wait(timeout=30)
    return run.result().column("id").to_pylist()


def _refs(stack: QueryStack, environment: str, table: str = "silver.x") -> set[str]:
    """Read the names of a table's snapshot references from its current metadata file.

    Parameters
    ----------
    stack
        The stack.
    environment
        The environment.
    table
        The table.

    Returns
    -------
    set of str
        ``main`` and the tags.
    """
    location = stack.admin.tables.get(environment, table).metadata_location
    assert location is not None
    with Session(stack.db) as session:
        resolved = StorageResolver(session).for_environment(environment)
        return set(read_metadata(resolved.file_io, location).refs)


def _deploy(stack: QueryStack, environment: str, files: dict[str, str]) -> str:
    """Register a repository with the given files and deploy it.

    Parameters
    ----------
    stack
        The stack.
    environment
        The environment.
    files
        Relative path to content; a path under ``checks/`` is a check like any file.

    Returns
    -------
    str
        The repository's name.
    """
    repository = unique_name()
    path = stack.services.roots / repository
    commit_files(path, {"lakana.yaml": _YAML, **files})
    stack.admin.repositories.create(repository, "local", str(path))
    deploy = stack.admin.deploys.create(environment, repository)
    _drain(stack)
    stack.admin.deploys.wait(environment, deploy.id, timeout=30)
    return repository


@dataclass(frozen=True)
class _Task:
    """A running task with checks, as its worker left it.

    Parameters
    ----------
    environment
        The environment's name.
    repository
        The repository's name.
    run_id
        The run.
    id
        The task run.
    claim
        The claim the (dead) worker holds.
    worker
        That worker's id, the lease owner.
    ref
        The task's table, ``silver.x``.
    principal
        The run's principal, without a run context.
    before
        The table's snapshot before the run; `None` when the run created the table.
    written
        The task run's *written* stamp; `None` for a task that was only claimed.
    spec
        The one check of the table.
    """

    environment: str
    repository: str
    run_id: uuid.UUID
    id: uuid.UUID
    claim: int
    worker: str
    ref: TableRef
    principal: PrincipalRef
    before: int | None
    written: Written | None
    spec: CheckSpec

    def actor(self, claim: int | None = None) -> PrincipalRef:
        """Return the run's principal acting for a claim of this task (the claim form).

        Parameters
        ----------
        claim
            The claim; this task's own when `None`.

        Returns
        -------
        PrincipalRef
            The principal with its run context.
        """
        context = RunContext(task_run_id=self.id, claim=self.claim if claim is None else claim)
        return PrincipalRef(
            id=self.principal.id,
            name=self.principal.name,
            type=self.principal.type,
            run_context=context,
        )


def _die(*args: object, **kwargs: object) -> worker_checks.Counted:
    """Stand in for `count_check`: the worker dies before it counts.

    Parameters
    ----------
    *args
        Ignored.
    **kwargs
        Ignored.

    Raises
    ------
    _Died
        Always.
    """
    raise _Died


def _task(
    stack: QueryStack,
    *,
    replaced: bool = True,
    write: bool = True,
    check: str = _CHECK,
    run_as_account: bool = False,
) -> _Task:
    """Deploy ``silver.x`` with one check, start a run, and leave its task `running`.

    The project is deployed from git and the run started through the REST API. With `write`
    the real worker builds the table (ids 1 to 3, through the query pipeline, fenced, so the
    write's own commit stamps the task run) and dies at its first count; without it the task
    is only claimed.

    Parameters
    ----------
    stack
        The stack.
    replaced
        Create ``silver.x`` with the ids 7 and 8 first, so that the run replaces a table;
        otherwise the run creates it.
    write
        Let the worker build the table before it dies.
    check
        The SQL of the check ``silver.x/small``.
    run_as_account
        Bind a new service account ``etl-<environment>`` to the deployment, with one grant
        (`DATA_ENGINEER` on the environment), so that the run's principal is one whose
        grants a test can revoke; otherwise the run is the administrator's.

    Returns
    -------
    _Task
        The task, held by `_WORKER` under a lease of `_LEASE` seconds.
    """
    environment = stack.environment()
    before = None
    if replaced:
        stack.admin.tables.create(environment, "silver.x", schema=IDS)
        before = stack.admin.tables.append(
            environment, "silver.x", pa.table({"id": pa.array([7, 8], pa.int64())})
        ).snapshot_id
    repository = _deploy(
        stack,
        environment,
        {
            "assets/silver/x.sql": "SELECT id FROM bronze.nums\n",
            "assets/gold/y.sql": "SELECT count(*) AS n FROM silver.x\n",
            "checks/silver/x/small.sql": check + "\n",
        },
    )
    if run_as_account:
        account = f"etl-{environment}"
        stack.admin.principals.create(account, "SERVICE_ACCOUNT")
        stack.admin.grants.grant(
            principal=account, role="DATA_ENGINEER", kind="ENVIRONMENT", environment=environment
        )
        stack.admin.deployments.set_run_as(environment, repository, account)
    run = stack.admin.runs.start(environment, repository)
    _tick(stack)
    engine = _engine(stack)
    if write:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(worker_checks, "count_check", _die)
            with pytest.raises(_Died):
                _drain(stack, engine)
    else:
        with Session(stack.db) as session:
            assert claim_task(session, _WORKER, _LEASE) is not None
    with Session(stack.db) as session:
        row = session.scalars(
            select(TaskRun).where(TaskRun.run_id == uuid.UUID(run.id), TaskRun.asset == "silver.x")
        ).one()
        assert (row.status, row.lease_owner, row.has_checks) == (TaskStatus.RUNNING, _WORKER, True)
        run_row = session.get(Run, row.run_id)
        assert run_row is not None
        principal = session.get(Principal, run_row.run_as_principal_id)
        version = session.get(JobVersion, run_row.job_version_id)
        assert principal is not None and version is not None
        (spec,) = checks_of(version.manifest_json, "silver.x")
        written = read_written(session, row.id)
        assert (written is not None) is write
        return _Task(
            environment=environment,
            repository=repository,
            run_id=row.run_id,
            id=row.id,
            claim=row.claim_count,
            worker=_WORKER,
            ref=TableRef.from_dotted(environment, "silver.x"),
            principal=PrincipalRef(id=principal.id, name=principal.name, type=principal.type),
            before=before,
            written=written,
            spec=spec,
        )


def _fail_the_check(stack: QueryStack, task: _Task) -> None:
    """Record the check as failed with one row, as the worker does after its count.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.
    """
    assert task.written is not None
    with Session(stack.db) as session:
        assert record_result(
            session,
            task_run_id=task.id,
            worker=task.worker,
            claim=task.claim,
            spec=task.spec,
            outcome=CheckOutcome.FAILED,
            failing_rows=1,
            error=None,
            table_id=task.written.table_id,
            checked_snapshot_id=task.written.snapshot_after,
            policy_applied=False,
            query_run_id=None,
            started_at=db_now(session),
        )


def _roll_back(
    session: Session,
    task: _Task,
    *,
    claim: int | None = None,
    written: Written | None = None,
    foreign_change: bool = False,
) -> Rollback:
    """Call the catalog's rollback as the worker does, on the given session.

    Parameters
    ----------
    session
        The session the rollback runs and commits on.
    task
        The task.
    claim
        The claim to act for; the task's own when `None`.
    written
        What to set back; the task's stamp when `None`.
    foreign_change
        Say that the task run has no write of its own and the table was changed by another.

    Returns
    -------
    Rollback
        What the catalog answered.
    """
    stamp = written or task.written
    assert stamp is not None
    catalog = CatalogService(session, StorageResolver(session), GrantAuthorization(session))
    return catalog.roll_back(
        task.actor(claim),
        task.ref,
        run_id=task.run_id,
        task_run_id=task.id,
        written=stamp,
        failed_checks=[task.spec.name],
        foreign_change=foreign_change,
    )


def _snapshot(stack: QueryStack, task: _Task) -> int | None:
    """Read the table's current snapshot through the REST API.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.

    Returns
    -------
    int or None
        The snapshot id.
    """
    return stack.admin.tables.get(task.environment, "silver.x").snapshot_id


def _outcomes(stack: QueryStack, task: _Task) -> list[tuple[CheckOutcome, RollbackOutcome | None]]:
    """Read the committed check results of the task.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.

    Returns
    -------
    list of tuple
        Each result's outcome and rollback outcome.
    """
    with Session(stack.db) as session:
        rows = session.scalars(select(CheckResult).where(CheckResult.task_run_id == task.id))
        return [(row.outcome, row.rollback_outcome) for row in rows]


def _rollbacks(stack: QueryStack, task: _Task) -> list[tuple[object, object]]:
    """Read the committed `TABLE_ROLLED_BACK` events of the task.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.

    Returns
    -------
    list of tuple
        The claim and the outcome each event names, oldest first.
    """
    with Session(stack.db) as session:
        events = session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.event_type == AuditEventType.TABLE_ROLLED_BACK,
                AuditEvent.securable_environment == task.environment,
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
        return [
            (e.detail["claim"], e.detail["outcome"])
            for e in events
            if e.detail["task_run_id"] == str(task.id)
        ]


def _stamp(stack: QueryStack, task: _Task) -> Written | None:
    """Read the task run's committed *written* stamp.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.

    Returns
    -------
    Written or None
        The stamp.
    """
    with Session(stack.db) as session:
        return read_written(session, task.id)


def _status(stack: QueryStack, task: _Task) -> tuple[TaskStatus, str | None, int]:
    """Read the task run's committed status, lease owner and attempt.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.

    Returns
    -------
    tuple
        Status, lease owner, attempt.
    """
    with Session(stack.db) as session:
        row = session.get(TaskRun, task.id)
        assert row is not None
        return row.status, row.lease_owner, row.attempt


def _expire_the_lease(stack: QueryStack, task: _Task) -> None:
    """Make the task's lease run out, by the database's clock.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.
    """
    with Session(stack.db) as session:
        session.execute(
            text(
                "UPDATE task_runs SET lease_expires_at = now() - interval '5 seconds' "
                "WHERE id = :id"
            ),
            {"id": task.id},
        )
        session.commit()


def _on_a_session(stack: QueryStack, call: Callable[[Session], object]) -> Callable[[], object]:
    """Wrap a call so that it gets a session of its own, opened in the thread that runs it.

    Parameters
    ----------
    stack
        The stack.
    call
        What to run with the session.

    Returns
    -------
    callable
        For `run_in_thread`.
    """

    def run() -> object:
        """Open the session and run the call.

        Returns
        -------
        object
            What the call returns.
        """
        with Session(stack.db) as session:
            return call(session)

    return run


class _SwapHold(HoldPoint):
    """Holds one named thread at the metadata pointer swap, before or after the real `UPDATE`.

    Parameters
    ----------
    monkeypatch
        Installs the wrapper around `commit_metadata_pointer` in the catalog service.
    thread
        The name of the thread to hold; any other caller (the server's own writes) passes.
    after
        Hold after the swap's `UPDATE` (the table's row is locked by the held transaction)
        instead of before it.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, thread: str, *, after: bool) -> None:
        super().__init__(_WAIT)
        self.backend: int | None = None
        real = service_module.commit_metadata_pointer
        held_by = self

        def swap(session: Session, table_id: uuid.UUID, **kwargs: object) -> None:
            """Swap, holding the named thread once.

            Parameters
            ----------
            session
                The writer's session.
            table_id
                The table.
            **kwargs
                Passed through.
            """
            mine = threading.current_thread().name == thread and not held_by.reached.is_set()
            if mine and not after:
                held_by._wait(session)
            real(session, table_id, **kwargs)  # ty: ignore[invalid-argument-type]
            if mine and after:
                held_by._wait(session)

        monkeypatch.setattr(service_module, "commit_metadata_pointer", swap)

    def _wait(self, session: Session) -> None:
        """Note the held backend, announce the hold and wait, bounded, to be released.

        Parameters
        ----------
        session
            The held session.
        """
        self.backend = backend_pid(session)
        self.wait_here()

    def held(self) -> int:
        """Block until the thread is held.

        Returns
        -------
        int
            The backend pid of the held session.
        """
        assert self.reached.wait(_WAIT), "the pointer swap was never reached"
        assert self.backend is not None
        return self.backend


def _cut_the_commit(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    *,
    applied: bool,
    before: Callable[[], None] | None = None,
    meanwhile: Callable[[], None] | None = None,
) -> None:
    """Make a session's first `commit` raise as a connection that dropped during `COMMIT`.

    What is real: with `applied` the transaction is committed by the server before the error
    is raised (an acknowledgement that never arrived); without it the transaction is rolled
    back (a `COMMIT` that never arrived). What is simulated: the error itself, raised here as
    the `OperationalError` SQLAlchemy makes of a driver error on a dead connection.

    Parameters
    ----------
    monkeypatch
        Installs the wrapper on the session.
    session
        The session.
    applied
        Whether the commit takes effect before the error.
    before
        Runs inside the transaction, right before it is committed or rolled back.
    meanwhile
        Runs after the transaction ended and before the error is raised: what other sessions
        do while the caller has not yet learned the outcome.

    Raises
    ------
    OperationalError
        From the first `commit`; later commits are the real ones.
    """
    real = session.commit
    calls: list[int] = []

    def commit() -> None:
        """Commit or roll back, then fail once.

        Raises
        ------
        OperationalError
            On the first call.
        """
        if calls:
            real()
            return
        calls.append(1)
        if before is not None:
            before()
        if applied:
            real()
        else:
            session.rollback()
        if meanwhile is not None:
            meanwhile()
        raise OperationalError("COMMIT", None, ConnectionResetError("connection reset"))

    monkeypatch.setattr(session, "commit", commit)


def _admin(stack: QueryStack) -> PrincipalRef:
    """Return the bootstrap administrator, a writer who is not the task.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    PrincipalRef
        The administrator, without a run context.
    """
    return PrincipalRef(id=stack.principal_id("admin"), name="admin", type=PrincipalType.USER)


def _append_99(stack: QueryStack, task: _Task) -> Callable[[Session], object]:
    """Build another writer's append of the id 99 to the task's table.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.

    Returns
    -------
    callable
        Runs the append on a session.
    """

    def append(session: Session) -> object:
        """Append through the catalog, in-process, as a principal that is not the task.

        Parameters
        ----------
        session
            The writer's session.

        Returns
        -------
        object
            The catalog's result.
        """
        catalog = CatalogService(session, StorageResolver(session), GrantAuthorization(session))
        return catalog.append(_admin(stack), task.ref, pa.table({"id": pa.array([99], pa.int64())}))

    return append


def test_a_rollback_sets_the_table_back_on_postgres(stack: QueryStack) -> None:
    """The baseline of every race below: alone, the rollback lands with all its records."""
    task = _task(stack)
    _fail_the_check(stack, task)
    assert task.written is not None and task.written.snapshot_before == task.before
    assert _snapshot(stack, task) == task.written.snapshot_after
    with Session(stack.db) as session:
        assert _roll_back(session, task) == Rollback(RollbackOutcome.ROLLED_BACK)
    assert _snapshot(stack, task) == task.before
    assert _outcomes(stack, task) == [(CheckOutcome.FAILED, RollbackOutcome.ROLLED_BACK)]
    assert _rollbacks(stack, task) == [(task.claim, "rolled_back")]
    assert _stamp(stack, task) is None
    assert _ids(stack, task.environment) == [7, 8]


def test_a_lease_renewal_does_not_wait_for_the_rollbacks_metadata_upload(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback takes no lock on the task run before its metadata file is uploaded.

    The upload is held; the task's own lease renewal (an `UPDATE` of the task run's row)
    must return meanwhile. Clearing the stamp before the upload would make it wait for as
    long as object storage takes, and a lease that cannot be renewed is a lost task.
    """
    task = _task(stack)
    _fail_the_check(stack, task)
    reached, release = threading.Event(), threading.Event()
    real = service_module.write_metadata

    def held_upload(*args: object, **kwargs: object) -> str:
        """Hold the rollback's upload until released; any other upload passes.

        Parameters
        ----------
        *args
            Passed through.
        **kwargs
            Passed through.

        Returns
        -------
        str
            What the real upload returns.
        """
        if threading.current_thread().name == "rollback":
            reached.set()
            assert release.wait(_WAIT), "the upload was never released"
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(service_module, "write_metadata", held_upload)
    rollback = run_in_thread("rollback", _on_a_session(stack, lambda s: _roll_back(s, task)))
    try:
        assert reached.wait(_WAIT), "the rollback never reached its upload"
        renewal = run_in_thread(
            "renewal",
            _on_a_session(
                stack, lambda s: renew_lease(s, task.id, task.worker, task.claim, _LEASE)
            ),
        )
        renewal[0].join(timeout=10)
        waited = renewal[0].is_alive()
        still_uploading = rollback[0].is_alive() and rollback[1] == []
    finally:
        release.set()
    assert not waited, "the lease renewal waited for the rollback's upload"
    assert still_uploading
    assert thread_result(*renewal) is LeaseState.HELD
    assert thread_result(*rollback) == Rollback(RollbackOutcome.ROLLED_BACK)
    assert _snapshot(stack, task) == task.before
    assert _stamp(stack, task) is None
    assert _rollbacks(stack, task) == [(task.claim, "rolled_back")]


def test_a_rollback_that_queues_behind_another_writers_commit_leaves_the_table_alone(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compare-and-swap between a write and a rollback of one table, the write first.

    Nothing is discarded. This is about the pointer's compare-and-swap, not about lock order:
    the other writer is an administrator without a run context, so it never locks the task
    run (the lock order of a rollback against the task's own row is the subject of
    `test_a_lease_renewal_does_not_wait_for_the_rollbacks_metadata_upload`).

    The other writer has moved the pointer and holds the table's row; the rollback, prepared
    on the snapshot the run wrote, waits at its own swap. When the writer commits, the
    rollback's compare-and-swap misses, it looks again and finds a table that is no longer
    what the run wrote.
    """
    task = _task(stack)
    _fail_the_check(stack, task)
    hold = _SwapHold(monkeypatch, "writer", after=True)
    writer = run_in_thread("writer", _on_a_session(stack, _append_99(stack, task)))
    try:
        blocker = hold.held()
        rollback = run_in_thread("rollback", _on_a_session(stack, lambda s: _roll_back(s, task)))
        wait_for_lock_waiter(stack.db, blocker)  # the rollback's swap waits for the writer
        assert rollback[1] == []
    finally:
        hold.release.set()
    assert cast("WriteResult", thread_result(*writer)).rows == 1
    assert thread_result(*rollback) == Rollback(RollbackOutcome.CHANGED_SINCE)
    assert _ids(stack, task.environment) == [1, 2, 3, 99]
    assert _outcomes(stack, task) == [(CheckOutcome.FAILED, RollbackOutcome.CHANGED_SINCE)]
    assert _rollbacks(stack, task) == [(task.claim, "changed_since")]
    assert _stamp(stack, task) == task.written  # nothing was set back, so the stamp stays


def test_a_write_that_queues_behind_a_rollbacks_commit_does_not_bring_the_data_back(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same compare-and-swap, the rollback first: another writer's append waits for it.

    As above, the other writer has no run context and locks only the table's row.


    The append was staged on the snapshot the rollback removes. It must not land on top of
    it: either it is refused as a conflict, or it is staged again on what the table is now.
    """
    task = _task(stack)
    _fail_the_check(stack, task)
    hold = _SwapHold(monkeypatch, "rollback", after=True)
    rollback = run_in_thread("rollback", _on_a_session(stack, lambda s: _roll_back(s, task)))
    try:
        blocker = hold.held()
        writer = run_in_thread("writer", _on_a_session(stack, _append_99(stack, task)))
        wait_for_lock_waiter(stack.db, blocker)  # the append's swap waits for the rollback
        assert writer[1] == []
    finally:
        hold.release.set()
    assert thread_result(*rollback) == Rollback(RollbackOutcome.ROLLED_BACK)
    appended = thread_result(*writer)
    assert isinstance(appended, ConflictError), appended
    assert _ids(stack, task.environment) == [7, 8]
    assert _snapshot(stack, task) == task.before
    assert _rollbacks(stack, task) == [(task.claim, "rolled_back")]


def test_a_rollback_whose_commit_landed_without_its_acknowledgement_is_recorded_once(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `COMMIT` is applied and the answer is lost: the outcome is the one that landed.

    Simulated: the error after the commit (see `_cut_the_commit`). Real: the commit, the
    read-back on a new transaction, and everything it finds.
    """
    task = _task(stack)
    _fail_the_check(stack, task)
    with Session(stack.db) as session:
        _cut_the_commit(monkeypatch, session, applied=True)
        assert _roll_back(session, task) == Rollback(RollbackOutcome.ROLLED_BACK)
    assert _snapshot(stack, task) == task.before
    assert _rollbacks(stack, task) == [(task.claim, "rolled_back")]  # one event, not two
    assert _outcomes(stack, task) == [(CheckOutcome.FAILED, RollbackOutcome.ROLLED_BACK)]
    assert _stamp(stack, task) is None


def test_a_rollback_whose_commit_never_arrived_says_refused_and_records_it(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the read-back: no event of this claim, so the table was not set back."""
    task = _task(stack)
    _fail_the_check(stack, task)
    assert task.written is not None
    with Session(stack.db) as session:
        _cut_the_commit(monkeypatch, session, applied=False)
        assert _roll_back(session, task) == Rollback(
            RollbackOutcome.FAILED, ROLLBACK_REFUSED_REASON
        )
    assert _snapshot(stack, task) == task.written.snapshot_after
    assert _rollbacks(stack, task) == [(task.claim, "failed")]
    assert _outcomes(stack, task) == [(CheckOutcome.FAILED, RollbackOutcome.FAILED)]
    assert _stamp(stack, task) == task.written


class _InFlight:
    """The rollback's own commit, answered with an error and still being applied by the server.

    The rollback's transaction is rolled back inside its `commit`, and a second connection
    then does what that transaction did and does not commit: it moves the table's pointer to
    the metadata file the rollback uploaded (so it holds the table's row, as a backend inside
    its `COMMIT` does) and inserts the rollback's audit event. The rollback's `commit` then
    raises, as on a dropped connection.

    Parameters
    ----------
    stack
        The stack.
    monkeypatch
        Installs the wrapper on the session.
    task
        The task that is rolled back.
    """

    def __init__(self, stack: QueryStack, monkeypatch: pytest.MonkeyPatch, task: _Task) -> None:
        self.stack = stack
        self.monkeypatch = monkeypatch
        self.task = task
        self.flight = Session(stack.db)
        self.reached = threading.Event()
        self.blocker: int | None = None
        self.table_id: uuid.UUID | None = None
        self.location: str | None = None

    def install(self, session: Session) -> None:
        """Make the session's first `commit` leave the commit in flight and raise.

        Parameters
        ----------
        session
            The rollback's session.
        """

        def meanwhile() -> None:
            """Redo the rollback's pointer move and audit event on the second connection."""
            self.flight.execute(
                text("UPDATE tables SET metadata_location = :l WHERE id = :id"),
                {"l": self.location, "id": self.table_id},
            )
            AuditService(self.flight).record(
                self.task.principal.id,
                AuditEventType.TABLE_ROLLED_BACK,
                securable=Securable.table_scope(
                    self.task.environment, self.task.ref.namespace, self.task.ref.name
                ),
                detail={
                    "task_run_id": str(self.task.id),
                    "claim": self.task.claim,
                    "outcome": "rolled_back",
                },
            )
            self.flight.flush()
            self.blocker = backend_pid(self.flight)
            self.reached.set()

        def before() -> None:
            """Remember the pointer the rollback's transaction has moved to."""
            row = session.execute(
                text(
                    "SELECT t.id, t.metadata_location FROM tables t "
                    "JOIN namespaces n ON n.id = t.namespace_id "
                    "JOIN environments e ON e.id = n.environment_id "
                    "WHERE e.name = :env AND t.name = 'x' AND n.path = 'silver'"
                ),
                {"env": self.task.environment},
            ).one()
            self.table_id, self.location = row.id, row.metadata_location

        _cut_the_commit(
            self.monkeypatch, session, applied=False, before=before, meanwhile=meanwhile
        )


def test_the_read_back_of_a_rollback_waits_for_a_commit_still_in_flight(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain read would answer "did not land" while the server still applies the commit."""
    task = _task(stack)
    _fail_the_check(stack, task)
    flight = _InFlight(stack, monkeypatch, task)

    def roll_back(session: Session) -> object:
        """Roll back on a session whose commit is left in flight.

        Parameters
        ----------
        session
            The rollback's session.

        Returns
        -------
        object
            What the catalog answered.
        """
        flight.install(session)
        return _roll_back(session, task)

    rollback = run_in_thread("rollback", _on_a_session(stack, roll_back))
    try:
        assert flight.reached.wait(_WAIT), "the rollback never reached its commit"
        assert flight.blocker is not None
        wait_for_lock_waiter(stack.db, flight.blocker)  # the read-back's locking read waits
        assert rollback[1] == []
        assert _rollbacks(stack, task) == []  # nothing is committed yet
        flight.flight.commit()
    finally:
        flight.flight.rollback()
        flight.flight.close()
    assert thread_result(*rollback) == Rollback(RollbackOutcome.ROLLED_BACK)
    assert _rollbacks(stack, task) == [(task.claim, "rolled_back")]  # and no second, "failed"
    assert _outcomes(stack, task) == [(CheckOutcome.FAILED, None)]  # the flight wrote none


def test_a_read_back_that_times_out_says_not_known_and_records_nothing(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other session sits in its transaction longer than the lock timeout.

    Whether the rollback took effect cannot be read then, and nothing may be recorded: an
    outcome written now could be wrong either way.
    """
    monkeypatch.setattr(service_module, "LOCK_TIMEOUT_SECONDS", 1)
    task = _task(stack)
    _fail_the_check(stack, task)
    flight = _InFlight(stack, monkeypatch, task)

    def roll_back(session: Session) -> object:
        """Roll back on a session whose commit is left in flight.

        Parameters
        ----------
        session
            The rollback's session.

        Returns
        -------
        object
            What the catalog answered.
        """
        flight.install(session)
        return _roll_back(session, task)

    rollback = run_in_thread("rollback", _on_a_session(stack, roll_back))
    try:
        rollback[0].join(timeout=20)
        ended = not rollback[0].is_alive()
        recorded = _rollbacks(stack, task)
        results = _outcomes(stack, task)
    finally:
        flight.flight.rollback()
        flight.flight.close()
    assert ended, "the read-back waited longer than its lock timeout"
    answer = thread_result(*rollback)
    assert isinstance(answer, Rollback), answer
    assert answer.outcome is RollbackOutcome.FAILED
    assert answer.reason is ROLLBACK_NOT_KNOWN_REASON
    assert recorded == []
    assert results == [(CheckOutcome.FAILED, None)]
    assert _stamp(stack, task) == task.written


@pytest.mark.parametrize("at", ["a statement", "the commit"])
def test_a_backend_cut_between_the_swap_and_the_commit_is_a_failed_rollback(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch, at: str
) -> None:
    """`pg_terminate_backend` of the rollback's backend, after the swap and before `COMMIT`.

    Nothing is simulated: the server ends the connection, and what the rollback sends next
    (the guard's locking read, or the `COMMIT` itself) gets whatever the driver raises for
    that: a `DBAPIError` or a raw `OSError`, depending on where the driver was. Whichever it
    is, the rollback answers `failed` with a reason, records that once, and leaves the
    session usable.
    """
    task = _task(stack)
    _fail_the_check(stack, task)
    assert task.written is not None
    raised: list[BaseException] = []

    def cut(session: Session, then: Callable[[], None]) -> None:
        """End the session's backend from another connection, then go on as the product does.

        Parameters
        ----------
        session
            The rollback's session, inside its transaction.
        then
            The product's next step.
        """
        victim = backend_pid(session)
        with stack.db.connect() as other:
            assert other.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": victim}
            ).scalar_one()
        try:
            then()
        except BaseException as error:
            raised.append(error)
            raise
        raised.append(AssertionError("what was sent after the cut did not fail"))

    with Session(stack.db) as session:
        real_pending = ProtectedWriteGuard.record_pending
        real_commit = session.commit

        def pending(guard: ProtectedWriteGuard) -> None:
            """Cut before the guard's locking read, once.

            Parameters
            ----------
            guard
                The guard.
            """
            if guard._session is session and not raised:
                cut(session, lambda: real_pending(guard))
            else:
                real_pending(guard)

        def commit() -> None:
            """Cut before the `COMMIT`, once."""
            if raised:
                real_commit()
            else:
                cut(session, real_commit)

        if at == "a statement":
            monkeypatch.setattr(ProtectedWriteGuard, "record_pending", pending)
        else:
            monkeypatch.setattr(session, "commit", commit)
        answer = _roll_back(session, task)
        # The session is usable afterwards: the worker goes on with it.
        assert session.execute(text("SELECT 1")).scalar_one() == 1
        session.rollback()
    (error,) = raised
    # Which class the driver raises is recorded, not required. Seen here with pg8000 1.31.5:
    # `sqlalchemy.exc.InterfaceError` around `pg8000.exceptions.InterfaceError("network
    # error")`, at both positions. A reset while the client waits for a reply can also come
    # out as a raw `ConnectionResetError` (the case `lakana_core.runs.leader` notes); the
    # rollback catches both, and what it then does is what is asserted.
    seen = f"{type(error).__name__} from {type(getattr(error, 'orig', None)).__name__}"
    assert isinstance(error, (DBAPIError, OSError)), seen
    assert answer == Rollback(RollbackOutcome.FAILED, ROLLBACK_REFUSED_REASON), seen
    assert _snapshot(stack, task) == task.written.snapshot_after
    assert _rollbacks(stack, task) == [(task.claim, "failed")]
    assert _outcomes(stack, task) == [(CheckOutcome.FAILED, RollbackOutcome.FAILED)]
    assert _stamp(stack, task) == task.written


def test_a_superseded_workers_failed_rollback_records_nothing_over_the_new_claims(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two real sessions: the old claim's commit fails; meanwhile the new claim rolled back.

    The old worker's `COMMIT` never arrives. Before it learns that, the scheduler reclaims
    the task, a second worker claims it and its rollback lands. The old worker then finds no
    event of its own claim; what it must not do is record "failed" over the outcome of the
    claim that holds the task now.
    """
    task = _task(stack)
    _fail_the_check(stack, task)
    assert task.written is not None
    second: list[object] = []

    def the_task_moves_on() -> None:
        """Reclaim the task, claim it as another worker, and let that claim roll back."""
        _expire_the_lease(stack, task)
        assert _tick(stack) >= 1  # back to pending, uncounted: it is written and unchecked
        _tick(stack)
        with Session(stack.db) as session:
            claimed = claim_task(session, "second-worker", _LEASE)
            assert claimed is not None and claimed.task_run_id == task.id
            second.append(claimed.claim)
        with Session(stack.db) as session:
            second.append(_roll_back(session, task, claim=claimed.claim))

    with Session(stack.db) as session:
        _cut_the_commit(monkeypatch, session, applied=False, meanwhile=the_task_moves_on)
        with pytest.raises(PermissionDenied) as refused:
            _roll_back(session, task)
    assert str(refused.value) == SUPERSEDED_MESSAGE
    assert second == [task.claim + 1, Rollback(RollbackOutcome.ROLLED_BACK)]
    assert _rollbacks(stack, task) == [(task.claim + 1, "rolled_back")]
    assert _outcomes(stack, task) == [(CheckOutcome.FAILED, RollbackOutcome.ROLLED_BACK)]
    assert _snapshot(stack, task) == task.before


@pytest.mark.parametrize("would_be", ["nothing_written", "changed_since", "foreign_change"])
def test_a_run_principal_without_grants_records_no_rollback_that_moves_no_pointer(
    stack: QueryStack, would_be: str
) -> None:
    """The rollback's own authorization is the only gate where no metadata commit follows.

    Three rollbacks that change no table and so never reach the catalog's commit, which
    would authorize again: one that finds nothing written, one that finds the table changed
    by another writer, and one the worker asks for as "changed by another". Each would still
    write an audit event and the outcome on the task's results. The run's principal lost its
    grants after the write, and its claim is live: it is refused as a principal without the
    grant, and nothing is recorded.
    """
    task = _task(stack, run_as_account=True)
    assert task.written is not None and task.principal.name == f"etl-{task.environment}"
    _fail_the_check(stack, task)
    written = task.written
    if would_be == "nothing_written":
        written = Written(written.table_id, written.snapshot_before, None, False)
    elif would_be == "changed_since":
        stack.admin.tables.append(
            task.environment, "silver.x", pa.table({"id": pa.array([99], pa.int64())})
        )
    now = _snapshot(stack, task)
    (grant,) = stack.admin.grants.list(principal_name=task.principal.name)
    stack.admin.grants.revoke(grant.id)
    with Session(stack.db) as session, pytest.raises(PermissionDenied) as refused:
        _roll_back(session, task, written=written, foreign_change=would_be == "foreign_change")
    assert str(refused.value) != SUPERSEDED_MESSAGE  # refused for the grant, not for the claim
    assert _status(stack, task) == (TaskStatus.RUNNING, task.worker, 1)
    assert _rollbacks(stack, task) == []
    assert _outcomes(stack, task) == [(CheckOutcome.FAILED, None)]
    assert _stamp(stack, task) == task.written
    assert _snapshot(stack, task) == now
    assert stack.events(task.environment, AuditEventType.PROTECTED_WRITE_DENIED) == []


# --- the run store and the worker's records against a reclaim --------------------------------


def _attempts(stack: QueryStack, task: _Task) -> list[AttemptOutcome | None]:
    """Read the outcomes of the task's attempts, oldest first.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.

    Returns
    -------
    list
        One outcome per attempt; `None` for an attempt that is still open.
    """
    with Session(stack.db) as session:
        return list(
            session.scalars(
                select(TaskAttempt.outcome)
                .where(TaskAttempt.task_run_id == task.id)
                .order_by(TaskAttempt.attempt, TaskAttempt.started_at)
            )
        )


def _record(stack: QueryStack, task: _Task) -> Callable[[Session], object]:
    """Build the worker's recording of the task's check as failed.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.

    Returns
    -------
    callable
        Runs `record_result` on a session and returns what it returns.
    """
    assert task.written is not None
    table_id, snapshot = task.written.table_id, task.written.snapshot_after

    def record(session: Session) -> object:
        """Record the result for the task's claim.

        Parameters
        ----------
        session
            The worker's session.

        Returns
        -------
        object
            Whether the result was recorded.
        """
        return record_result(
            session,
            task_run_id=task.id,
            worker=task.worker,
            claim=task.claim,
            spec=task.spec,
            outcome=CheckOutcome.FAILED,
            failing_rows=1,
            error=None,
            table_id=table_id,
            checked_snapshot_id=snapshot,
            policy_applied=False,
            query_run_id=None,
            started_at=db_now(session),
        )

    return record


def _check_failed_events(stack: QueryStack, task: _Task) -> int:
    """Count the committed `CHECK_FAILED` events of the task's environment.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.

    Returns
    -------
    int
        The count.
    """
    return len(stack.events(task.environment, AuditEventType.CHECK_FAILED))


def test_a_result_is_not_recorded_for_a_claim_the_scheduler_is_taking_back(
    stack: QueryStack,
) -> None:
    """`record_result` reads the task run `FOR SHARE`: behind a reclaim it waits, then sees it.

    The real tick holds the task's row (it is paused right before the `UPDATE` that puts the
    task back) when the stale worker tries to record. Without the locking read the worker
    would read the row as its snapshot shows it, still its own, and record a result (and a
    `CHECK_FAILED` event) for a claim that no longer exists.
    """
    task = _task(stack)
    _expire_the_lease(stack, task)
    with StatementPause(stack.db, "UPDATE task_runs SET status=", "tick") as pause:
        tick = run_in_thread("tick", lambda: _tick(stack))
        blocker = pause.wait()
        recorder = run_in_thread("recorder", _on_a_session(stack, _record(stack, task)))
        wait_for_lock_waiter(stack.db, blocker)  # the recorder's locking read waits
        assert recorder[1] == []
    assert thread_result(*tick) == 2  # back to pending as written and unchecked, and ready again
    assert thread_result(*recorder) is False
    assert _outcomes(stack, task) == []
    assert _check_failed_events(stack, task) == 0
    assert _status(stack, task) == (TaskStatus.READY, None, 1)
    assert _stamp(stack, task) == task.written
    assert _attempts(stack, task) == [AttemptOutcome.LOST]


def test_a_reclaim_waits_for_a_result_that_is_being_recorded(stack: QueryStack) -> None:
    """The other order: the worker holds the task run's row, the tick waits and then applies."""
    task = _task(stack)
    _expire_the_lease(stack, task)
    with StatementPause(stack.db, "INSERT INTO check_results", "recorder") as pause:
        recorder = run_in_thread("recorder", _on_a_session(stack, _record(stack, task)))
        blocker = pause.wait()
        tick = run_in_thread("tick", lambda: _tick(stack))
        wait_for_lock_waiter(stack.db, blocker)  # the reclaim's lock waits for the recorder
        assert tick[1] == []
    assert thread_result(*recorder) is True
    assert thread_result(*tick) == 2
    assert _outcomes(stack, task) == [(CheckOutcome.FAILED, None)]
    assert _check_failed_events(stack, task) == 1
    assert _status(stack, task) == (TaskStatus.READY, None, 1)


def _mark(task: _Task, written: Written) -> Callable[[Session], object]:
    """Build a Python task's recording of what its process wrote.

    Parameters
    ----------
    task
        The task.
    written
        What to stamp.

    Returns
    -------
    callable
        Runs `mark_written` on a session and returns what it returns.
    """

    def mark(session: Session) -> object:
        """Stamp the task run for the task's claim.

        Parameters
        ----------
        session
            The worker's session.

        Returns
        -------
        object
            Whether the stamp was written.
        """
        return mark_written(session, task.id, task.worker, task.claim, written)

    return mark


def _a_written(stack: QueryStack, task: _Task) -> Written:
    """Build a stamp for a task that was only claimed: what a Python process would have written.

    Parameters
    ----------
    stack
        The stack.
    task
        The task, whose table exists.

    Returns
    -------
    Written
        The table, its snapshot as "before", and a made-up snapshot as "after".
    """
    table_id = uuid.UUID(stack.admin.tables.get(task.environment, "silver.x").uuid)
    return Written(table_id, task.before, 4242, False)


def test_a_stamp_that_commits_while_the_reclaim_waits_makes_the_reclaim_miss(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mark_written` against a reclaim, the stamp first.

    A Python task has no write run, so the stamp is the only thing that says "written". The
    tick read the task before the stamp was committed and computed an ordinary retry, which
    would build the table again. Its lock waits for the stamp's transaction; the statement
    after the lock must then see the stamp and miss.
    """
    task = _task(stack, write=False)
    written = _a_written(stack, task)
    _expire_the_lease(stack, task)
    held, release, pid = threading.Event(), threading.Event(), []

    def mark(session: Session) -> object:
        """Stamp the task run, held before the commit of the stamp.

        Parameters
        ----------
        session
            The marker's session.

        Returns
        -------
        object
            What `mark_written` returns.
        """
        real = session.commit

        def commit() -> None:
            """Hold the stamp's transaction open before its commit."""
            pid.append(backend_pid(session))
            held.set()
            assert release.wait(_WAIT), "the stamp was never released"
            real()

        monkeypatch.setattr(session, "commit", commit)
        return _mark(task, written)(session)

    marker = run_in_thread("marker", _on_a_session(stack, mark))
    try:
        assert held.wait(_WAIT), "the stamp never reached its commit"
        tick = run_in_thread("tick", lambda: _tick(stack))
        wait_for_lock_waiter(stack.db, pid[0])  # the reclaim's lock waits for the stamp
        assert tick[1] == []
    finally:
        release.set()
    assert thread_result(*marker) is True
    assert thread_result(*tick) == 0  # the stale retry missed
    assert _status(stack, task) == (TaskStatus.RUNNING, task.worker, 1)
    assert _stamp(stack, task) == written
    assert _attempts(stack, task) == [None]
    # The next tick knows the task as written and unchecked: back, with its stamp, uncounted.
    assert _tick(stack) == 2
    assert _status(stack, task) == (TaskStatus.READY, None, 1)
    assert _stamp(stack, task) == written


def test_a_stamp_that_waits_behind_the_reclaim_is_not_written(stack: QueryStack) -> None:
    """`mark_written` against a reclaim, the reclaim first: the stale worker stamps nothing."""
    task = _task(stack, write=False)
    written = _a_written(stack, task)
    _expire_the_lease(stack, task)
    with StatementPause(stack.db, "UPDATE task_runs SET status=", "tick") as pause:
        tick = run_in_thread("tick", lambda: _tick(stack))
        blocker = pause.wait()
        marker = run_in_thread("marker", _on_a_session(stack, _mark(task, written)))
        wait_for_lock_waiter(stack.db, blocker)  # the stamp's `UPDATE` waits for the reclaim
        assert marker[1] == []
    assert thread_result(*tick) == 2
    assert thread_result(*marker) is False
    assert _stamp(stack, task) is None
    assert _status(stack, task) == (TaskStatus.READY, None, 1)
    assert _attempts(stack, task) == [AttemptOutcome.LOST]


def test_a_written_task_is_never_taken_for_succeeded_or_for_a_retry_on_postgres(
    stack: QueryStack,
) -> None:
    """The predicates behind "written and unchecked", as PostgreSQL evaluates them.

    `status NOT IN (the terminal ones)` on the enum column, the evidence `EXISTS`, and the
    reclaims that carry them: a stale reclaim to `succeeded` (what the sweep does for a task
    without checks) and a stale ordinary retry both miss a written task; only the reclaim
    that says "unchecked" applies.
    """
    task = _task(stack)
    with Session(stack.db) as session:
        assert awaits_checks(session, task.id) is True
        # Without the stamp the write run's evidence still says so (a rollback clears the stamp).
        session.execute(update(TaskRun).where(TaskRun.id == task.id).values(written_table_id=None))
        assert awaits_checks(session, task.id) is True
        for ended in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            session.execute(update(TaskRun).where(TaskRun.id == task.id).values(status=ended))
            assert awaits_checks(session, task.id) is False
        session.execute(
            update(TaskRun).where(TaskRun.id == task.id).values(status=TaskStatus.PENDING)
        )
        assert awaits_checks(session, task.id) is True
        session.rollback()
    _expire_the_lease(stack, task)

    def stale(status: TaskStatus) -> TaskTransition:
        """Build a reclaim as a tick would have computed it before the write landed.

        Parameters
        ----------
        status
            Where the stale reclaim wants the task.

        Returns
        -------
        TaskTransition
            A reclaim that is not marked "unchecked".
        """
        return TaskTransition(
            task.run_id,
            task.id,
            TaskStatus.RUNNING,
            status,
            expected_owner=task.worker,
            expected_claim=task.claim,
            lost=True,
        )

    with Session(stack.db) as session:
        now = db_now(session)
        assert apply_transitions(session, [stale(TaskStatus.SUCCEEDED)], now) is False
        assert apply_transitions(session, [stale(TaskStatus.PENDING)], now) is False
        assert apply_transitions(session, [stale(TaskStatus.FAILED)], now) is False
    assert _status(stack, task) == (TaskStatus.RUNNING, task.worker, 1)
    assert _attempts(stack, task) == [None]
    assert _tick(stack) == 2
    assert _status(stack, task) == (TaskStatus.READY, None, 1)
    assert _attempts(stack, task) == [AttemptOutcome.LOST]
    assert _stamp(stack, task) == task.written
    with Session(stack.db) as session:
        downstream = session.scalars(
            select(TaskRun.status).where(TaskRun.run_id == task.run_id, TaskRun.asset == "gold.y")
        ).one()
    assert downstream is TaskStatus.PENDING  # never ready: nothing was checked


# --- a Python task's own writes: the baseline and the tip ------------------------------------


def _with_a_baseline(stack: QueryStack, task: _Task) -> Baseline:
    """Record the task run's baseline as the worker does before a Python task's first process.

    Parameters
    ----------
    stack
        The stack.
    task
        A task that was only claimed.

    Returns
    -------
    Baseline
        The stored baseline: the table's snapshot before the run, or "would publish".
    """
    with Session(stack.db) as session:
        stored = record_baseline(
            session, task.id, task.worker, task.claim, Baseline(task.before, task.before is None)
        )
    assert stored is not None
    return stored


def _tip(stack: QueryStack, task: _Task) -> int | None:
    """Read the committed tip of the task run's own chain of writes, as stored.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.

    Returns
    -------
    int or None
        `task_runs.own_snapshot_id`.
    """
    with Session(stack.db) as session:
        return session.scalars(select(TaskRun.own_snapshot_id).where(TaskRun.id == task.id)).one()


def _replace(
    task: _Task, ids: list[int], expected: int | str | None
) -> Callable[[Session], object]:
    """Build a Python task's own replace of its table, under its claim.

    Parameters
    ----------
    task
        The task.
    ids
        The rows to write.
    expected
        The snapshot the write is pinned to, ``"none"`` for a table without one, or `None`
        for no pin.

    Returns
    -------
    callable
        Runs the replace on a session, as the route does for a bound task token.
    """

    def replace(session: Session) -> object:
        """Replace through the catalog with the claim-form run context.

        Parameters
        ----------
        session
            The session.

        Returns
        -------
        object
            The catalog's result.
        """
        catalog = CatalogService(session, StorageResolver(session), GrantAuthorization(session))
        return catalog.replace(
            task.actor(),
            task.ref,
            pa.table({"id": pa.array(ids, pa.int64())}),
            expected_snapshot_id=cast("service_module.ExpectedSnapshot | None", expected),
        )

    return replace


def test_the_tip_follows_a_task_runs_own_writes_from_its_baseline(stack: QueryStack) -> None:
    """`IS NOT DISTINCT FROM` in the tip's `UPDATE`, for a baseline with and without a snapshot.

    A table the task creates has a baseline of "no snapshot": `baseline_snapshot_id = NULL`
    would never be true, and the task run would look as if it had written nothing.
    """
    created = _task(stack, replaced=False, write=False)
    assert _with_a_baseline(stack, created) == Baseline(None, True)
    with Session(stack.db) as session:
        first = cast("WriteResult", _replace(created, [1], "none")(session))
    assert first.snapshot_id is not None and _tip(stack, created) == first.snapshot_id
    with Session(stack.db) as session:
        second = cast("WriteResult", _replace(created, [1, 2], first.snapshot_id)(session))
    assert _tip(stack, created) == second.snapshot_id  # staged on the tip: the chain goes on
    with Session(stack.db) as session:
        assert read_own_tip(session, created.id) is not None
        assert read_baseline(session, created.id) == Baseline(None, True)

    replaced = _task(stack, write=False)
    assert _with_a_baseline(stack, replaced) == Baseline(replaced.before, False)
    with Session(stack.db) as session:
        third = cast("WriteResult", _replace(replaced, [1], replaced.before)(session))
    assert _tip(stack, replaced) == third.snapshot_id != OWN_TIP_NO_SNAPSHOT


def test_a_foreign_commit_between_a_tasks_staging_and_its_swap_is_never_under_the_tip(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real concurrent commit by another writer, between the task's staging and its swap.

    The task's replace is staged on the baseline and held right before its pointer swap;
    another writer's append commits; the task's swap then loses the compare-and-swap. If the
    task writes again, on top of the other writer's snapshot, that write lands, and the tip
    must stay empty: what is in the table is no longer the task run's alone, and a rollback
    that named this write as its "after" would discard the other writer's rows with it.
    """
    task = _task(stack, write=False)
    _with_a_baseline(stack, task)
    hold = _SwapHold(monkeypatch, "task", after=False)
    writer = run_in_thread("task", _on_a_session(stack, _replace(task, [1, 2, 3], task.before)))
    try:
        hold.held()
        appended = stack.admin.tables.append(
            task.environment, "silver.x", pa.table({"id": pa.array([99], pa.int64())})
        )
    finally:
        hold.release.set()
    lost = thread_result(*writer)
    assert isinstance(lost, ConflictError), lost
    assert _tip(stack, task) is None
    assert _snapshot(stack, task) == appended.snapshot_id
    with Session(stack.db) as session:
        again = cast("WriteResult", _replace(task, [1, 2, 3], appended.snapshot_id)(session))
    assert _snapshot(stack, task) == again.snapshot_id
    assert _tip(stack, task) is None  # not this task run's chain: staged on a foreign commit
    # The worker then asks for "changed by another", and the catalog touches nothing.
    with Session(stack.db) as session:
        catalog = CatalogService(session, StorageResolver(session), GrantAuthorization(session))
        answer = catalog.roll_back(
            task.actor(),
            task.ref,
            run_id=task.run_id,
            task_run_id=task.id,
            written=Written(again.table_id, task.before, None, False),
            failed_checks=[],
            foreign_change=True,
        )
    assert answer.outcome is RollbackOutcome.CHANGED_SINCE
    assert _snapshot(stack, task) == again.snapshot_id
    assert _ids(stack, task.environment) == [1, 2, 3]


@pytest.mark.parametrize("applied", [True, False], ids=["landed", "never arrived"])
def test_the_tip_and_the_pointer_swap_land_together_when_the_acknowledgement_is_lost(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch, applied: bool
) -> None:
    """A Python task's write whose `COMMIT` is answered with an error.

    The tip is written in the write's own transaction: whatever became of the commit, the
    tip names the table's snapshot or nothing at all. Simulated: the error (`_cut_the_commit`).
    """
    task = _task(stack, write=False)
    _with_a_baseline(stack, task)
    with Session(stack.db) as session:
        _cut_the_commit(monkeypatch, session, applied=applied)
        try:
            outcome: object = _replace(task, [1, 2, 3], task.before)(session)
        except Exception as error:
            outcome = error
    now = _snapshot(stack, task)
    if applied:
        assert isinstance(outcome, WriteResult), outcome
        assert now == outcome.snapshot_id != task.before
        assert _tip(stack, task) == now
    else:
        assert isinstance(outcome, OperationalError), outcome
        assert now == task.before
        assert _tip(stack, task) is None


# --- Python assets through the worker, with a scripted process -------------------------------

_PY_ASSET = (
    "from lakana import asset\n\n\n"
    "@asset(inputs={{'source': '{source}'}})\n"
    "def build(source):\n"
    "    return source\n"
)

Step = Callable[[PythonTask], RunOutcome]
"""What one attempt's "process" does."""


@dataclass
class _Scripted:
    """A `Runner` whose attempts are functions of the test, one per attempt, in order.

    Parameters
    ----------
    steps
        What each attempt does.
    seen
        Every task it was given.
    """

    steps: list[Step]
    seen: list[PythonTask] = field(default_factory=list)

    def run(
        self, task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
    ) -> RunOutcome:
        """Run the next step.

        Parameters
        ----------
        task
            The task.
        on_log
            The log sink.
        stop
            The stop signal; not used.

        Returns
        -------
        RunOutcome
            What the step reports.
        """
        self.seen.append(task)
        on_log(f"attempt {len(self.seen)}\n".encode())
        return self.steps[len(self.seen) - 1](task)


def _python_engine(stack: QueryStack, root: Path, steps: list[Step]) -> tuple[RunEngine, _Scripted]:
    """Build a run engine whose Python runtime runs scripted attempts instead of processes.

    Real: the worker's whole Python path (the code tarball from object storage, the lock
    check, the baseline, the bound task token, the classification of the outcome). Not real:
    `uv` (no venv is built) and the harness process.

    Parameters
    ----------
    stack
        The stack.
    root
        A directory of the test for the attempt directories.
    steps
        What each attempt does.

    Returns
    -------
    tuple
        The engine and its runner.
    """

    def no_venv(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
    ) -> VenvLease:
        """Stand in for the venv cache: no build; a lease with a real descriptor to close.

        Parameters
        ----------
        settings
            Ignored.
        sha
            The lock's digest.
        project
            The unpacked code directory.
        on_log
            Ignored.
        indexes
            Ignored.

        Returns
        -------
        VenvLease
            A lease on a directory under `root`.
        """
        read_end, write_end = os.pipe()
        os.close(read_end)
        return VenvLease(
            path=root / "venvs" / sha, python=Path("/nonexistent"), descriptor=write_end
        )

    runner = _Scripted(steps)
    engine = _engine(stack)
    engine.python = PythonRuntime(
        support=PythonSupport(server_url=stack.url, uv=shutil.which("uv") or "/usr/bin/uv"),
        work_dir=root / "work",
        venvs=VenvSettings(uv="uv", cache=root / "venvs", uv_cache=root / "uv"),
        runner=runner,
        kill_grace=0.2,
        acquire_venv=no_venv,
    )
    return engine, runner


def _python_files(source: str, table: str = "silver.x") -> dict[str, str]:
    """Build the files of a project with one Python asset.

    Parameters
    ----------
    source
        The table the asset reads.
    table
        The asset's own table.

    Returns
    -------
    dict
        The asset, a `pyproject.toml` and a `uv.lock` that names this worker's SDK version.
    """
    lock = (
        'version = 1\nrequires-python = ">=3.11"\n\n'
        f'[[package]]\nname = "lakana"\nversion = "{package_version("lakana-worker")}"\n\n'
        '[[package]]\nname = "polars"\nversion = "1.44.2"\n'
    )
    return {
        f"assets/{table.replace('.', '/')}.py": _PY_ASSET.format(source=source),
        "pyproject.toml": _PYPROJECT,
        "uv.lock": lock,
    }


def _upload(ids: list[int]) -> Callable[[PythonTask], SdkWriteResult]:
    """Build the upload of a task's table as the harness does it.

    Parameters
    ----------
    ids
        The rows to write.

    Returns
    -------
    callable
        Replaces the task's table over HTTP with the attempt's token, pinned to the snapshot
        the worker read before the "process" started.
    """

    def upload(task: PythonTask) -> SdkWriteResult:
        """Replace the task's table.

        Parameters
        ----------
        task
            The task.

        Returns
        -------
        WriteResult
            What the server answered.
        """
        with lakana.Client(task.server_url, task.token, timeout=60.0) as client:
            return client.tables.replace(
                task.environment,
                task.table,
                pa.table({"id": pa.array(ids, pa.int64())}),
                expected_snapshot_id=task.expected_snapshot_id,
            )

    return upload


def _writes_then_fails(ids: list[int]) -> Step:
    """Build an attempt that commits to its table and then fails in a way that is retried.

    Parameters
    ----------
    ids
        The rows it writes first.

    Returns
    -------
    Step
        The attempt.
    """

    def step(task: PythonTask) -> RunOutcome:
        """Write, then report a failure that may be retried.

        Parameters
        ----------
        task
            The task.

        Returns
        -------
        RunOutcome
            The failure.
        """
        _upload(ids)(task)
        return RunOutcome(succeeded=False, error_message="the function raised after its write")

    return step


def _writes(ids: list[int]) -> Step:
    """Build an attempt that commits to its table and reports success.

    Parameters
    ----------
    ids
        The rows it writes.

    Returns
    -------
    Step
        The attempt.
    """

    def step(task: PythonTask) -> RunOutcome:
        """Write, then report success.

        Parameters
        ----------
        task
            The task.

        Returns
        -------
        RunOutcome
            The success, with the snapshot and the rows the server answered.
        """
        written = _upload(ids)(task)
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    return step


def _fails_for_good(task: PythonTask) -> RunOutcome:
    """Fail without writing, in a way no retry can help.

    Parameters
    ----------
    task
        The task.

    Returns
    -------
    RunOutcome
        A failure that is not retryable.
    """
    return RunOutcome(succeeded=False, error_message="the function cannot work", retryable=False)


def _python_project(stack: QueryStack, retries: int = 1) -> tuple[str, str, int]:
    """Deploy a Python asset ``silver.x`` with the check over an existing table (ids 7, 8).

    Parameters
    ----------
    stack
        The stack.
    retries
        The project's default number of retries; 0 makes the first attempt the last.

    Returns
    -------
    tuple
        The environment, the repository and the table's snapshot before any run.
    """
    environment = stack.environment()
    stack.admin.tables.create(environment, "silver.x", schema=IDS)
    before = stack.admin.tables.append(
        environment, "silver.x", pa.table({"id": pa.array([7, 8], pa.int64())})
    ).snapshot_id
    assert before is not None
    repository = _deploy(
        stack,
        environment,
        {
            **_python_files("bronze.nums"),
            "checks/silver/x/small.sql": _CHECK + "\n",
            "lakana.yaml": f"defaults:\n  retry_delay: 0\n  retries: {retries}\n",
        },
    )
    return environment, repository, before


def test_a_scripted_python_assets_second_attempt_is_rolled_back_to_before_the_first(
    stack: QueryStack, tmp_path: Path
) -> None:
    """Scripted process: attempt 1 writes and fails; attempt 2 writes and fails its check.

    The table goes back to what it was before the task run, not to what attempt 1 left: the
    baseline is recorded once, and the rollback names the tip of the task run's own writes,
    which is the snapshot attempt 2 wrote. The rollback that lands clears the stamp and the
    tip with it: the task run's write is no longer in the table.
    """
    environment, repository, before = _python_project(stack)
    second: list[RunOutcome] = []

    def writes_and_remembers(task: PythonTask) -> RunOutcome:
        """Write as attempt 2 does, keeping what the server answered to the upload.

        Parameters
        ----------
        task
            The task.

        Returns
        -------
        RunOutcome
            The success, with the snapshot the upload made.
        """
        second.append(_writes([4, 5])(task))
        return second[0]

    engine, runner = _python_engine(
        stack, tmp_path, [_writes_then_fails([1, 2, 3]), writes_and_remembers]
    )
    run = stack.admin.runs.start(environment, repository)
    _tick(stack)
    _drain(stack, engine)
    first = task_row(stack.db, run.id)
    assert (first.status, first.attempt, first.baseline_snapshot_id) == (
        TaskStatus.PENDING,
        1,
        before,
    )
    after_first = stack.admin.tables.get(environment, "silver.x").snapshot_id
    assert after_first not in (None, before) and first.own_snapshot_id == after_first
    assert runner.seen[0].expected_snapshot_id == before
    _tick(stack)
    _drain(stack, engine)
    done = task_row(stack.db, run.id)
    assert (done.status, done.attempt, done.baseline_snapshot_id) == (TaskStatus.FAILED, 2, before)
    assert runner.seen[1].expected_snapshot_id == after_first
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id == before
    assert _ids(stack, environment) == [7, 8]
    (result,) = stack.admin.checks.for_run(environment, run.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
        "failed",
        2,
        "rolled_back",
    )
    (wrote,) = second
    assert wrote.snapshot_id not in (None, before, after_first)
    assert result.checked_snapshot_id == wrote.snapshot_id  # the check read attempt 2's rows
    (event,) = stack.events(environment, AuditEventType.TABLE_ROLLED_BACK)
    assert (event.detail["before"], event.detail["after"]) == (before, wrote.snapshot_id)
    assert done.error_message is not None
    assert ROLLBACK_SENTENCES[RollbackOutcome.ROLLED_BACK] in done.error_message
    assert _refs(stack, environment) == {"main", failed_snapshot_tag(uuid.UUID(run.id))}
    # The rollback landed: neither the stamp nor the tip is left; the baseline stays.
    assert (done.written_table_id, done.written_snapshot_after, done.own_snapshot_id) == (
        None,
        None,
        None,
    )


def test_a_scripted_python_asset_that_fails_for_good_after_a_foreign_commit_leaves_the_table(
    stack: QueryStack, tmp_path: Path
) -> None:
    """Scripted process: attempt 1 writes and fails, another writer appends, attempt 2 fails.

    Nobody would replace the unchecked rows of attempt 1 now, so the worker asks for the
    table to be set back. The table is not as this task run left it, so the catalog answers
    `changed_since`: the other writer's row stays, and so do the rows it was appended to.
    """
    environment, repository, before = _python_project(stack)
    engine, _ = _python_engine(stack, tmp_path, [_writes_then_fails([1, 2, 3]), _fails_for_good])
    run = stack.admin.runs.start(environment, repository)
    _tick(stack)
    _drain(stack, engine)
    own = task_row(stack.db, run.id).own_snapshot_id
    foreign = stack.admin.tables.append(
        environment, "silver.x", pa.table({"id": pa.array([99], pa.int64())})
    ).snapshot_id
    _tick(stack)
    _drain(stack, engine)
    done = task_row(stack.db, run.id)
    assert (done.status, done.attempt, done.own_snapshot_id) == (TaskStatus.FAILED, 2, own)
    changed = ROLLBACK_SENTENCES[RollbackOutcome.CHANGED_SINCE].format(
        unchecked="that was not checked"
    )
    assert done.error_message == f"the function cannot work. {changed}"
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id == foreign
    assert _ids(stack, environment) == [1, 2, 3, 99]
    (event,) = stack.events(environment, AuditEventType.TABLE_ROLLED_BACK)
    assert (event.detail["outcome"], event.detail["before"], event.detail["after"]) == (
        "changed_since",
        before,
        own,
    )
    assert stack.admin.checks.for_run(environment, run.id) == []


def _reads_its_input_as_the_harness_does(task: PythonTask) -> RunOutcome:
    """Read the task's one input with the harness's own code, and report as the harness does.

    Parameters
    ----------
    task
        The task.

    Returns
    -------
    RunOutcome
        A success if the read worked (the asset would then go on); otherwise the failure
        record the harness writes for that error, with its own answer to "retry?".
    """
    description = SimpleNamespace(environment=task.environment, request_timeout_seconds=60.0)
    ((parameter, table),) = task.inputs.items()
    with lakana.Client(task.server_url, task.token, timeout=60.0) as client:
        try:
            harness._read_input(
                client,
                cast("harness.TaskDescription", description),
                parameter,
                table,
                "float",
            )
        except harness.InputError as error:
            record = harness._failure_record(harness.Failure("inputs", error), task.token)
            return RunOutcome(
                succeeded=False,
                error_message=cast("str", record["message"]),
                error_type=cast("str", record["error_type"]),
                retryable=cast("bool", record["retryable"]),
            )
    return RunOutcome(succeeded=True)


def test_a_scripted_python_asset_whose_input_is_not_published_fails_once_and_says_why(
    stack: QueryStack, tmp_path: Path
) -> None:
    """Scripted process: the input was never published; the asset that reads it fails once.

    The read is the harness's own function, run in this process with the attempt's real task
    token; the worker serves the query meanwhile, as it does beside a real task process.
    """
    environment = stack.environment()
    repository = _deploy(
        stack,
        environment,
        {
            "assets/silver/x.sql": "SELECT id FROM bronze.nums\n",
            "checks/silver/x/small.sql": _CHECK + "\n",
            **_python_files("silver.x", "gold.scores"),
            "checks/gold/scores/any.sql": "SELECT id FROM gold.scores WHERE id < 0\n",
        },
    )
    engine, runner = _python_engine(stack, tmp_path, [_reads_its_input_as_the_harness_does] * 2)
    first = stack.admin.runs.start(environment, repository)
    for _ in range(3):
        _tick(stack)
        _drain(stack, engine)
    built = stack.admin.runs.get(environment, first.id)
    assert {t.asset: t.status for t in built.tasks} == {
        "silver.x": "failed",
        "gold.scores": "upstream_failed",
    }
    table = stack.admin.tables.get(environment, "silver.x")
    assert table.state == "not_published" and runner.seen == []
    with Session(stack.db) as session:
        why = not_published_message(
            session, "silver.x", uuid.UUID(table.uuid), uuid.UUID(first.id), environment
        )

    second = stack.admin.runs.start(environment, repository, select=["gold.scores"])
    for _ in range(3):
        _tick(stack)
        _drain(stack, engine)
    done = stack.admin.runs.get(environment, second.id)
    (task,) = done.tasks
    assert (done.status, task.asset, task.status, task.attempt, task.max_attempts) == (
        "failed",
        "gold.scores",
        "failed",
        1,
        2,
    )
    assert task.error_message == f"reading input 'source' (silver.x) failed: {why}"
    assert len(runner.seen) == 1  # one process, although an attempt was left
    assert stack.admin.checks.for_run(environment, second.id) == []
    assert len(stack.events(environment, AuditEventType.TABLE_ROLLED_BACK)) == 1  # the input's


# --- A worker lost after it may have written: the next claim only sets the table back -------


def _vanish(*args: object, **kwargs: object) -> bool:
    """Stand in for a worker call after which the worker is gone.

    Parameters
    ----------
    *args
        Ignored.
    **kwargs
        Ignored.

    Raises
    ------
    _Died
        Always.
    """
    raise _Died


def _expire(stack: QueryStack, task_id: uuid.UUID) -> None:
    """Make a task's lease run out, by the database's clock.

    Parameters
    ----------
    stack
        The stack.
    task_id
        The task run.
    """
    with Session(stack.db) as session:
        session.execute(
            text(
                "UPDATE task_runs SET lease_expires_at = now() - interval '5 seconds' "
                "WHERE id = :id"
            ),
            {"id": task_id},
        )
        session.commit()


def _lost_python_run(
    stack: QueryStack, tmp_path: Path, step: Step, gone_at: tuple[object, str]
) -> tuple[str, str, int]:
    """Run a Python asset with checks on its last attempt until its worker is lost.

    Parameters
    ----------
    stack
        The stack.
    tmp_path
        For the attempt directories.
    step
        What the asset's process does.
    gone_at
        The module and the name of the worker call after which the worker is gone: it is
        replaced by `_vanish` while the task runs.

    Returns
    -------
    tuple
        The environment, the run id and the table's snapshot before the run. The task's lease
        is expired.
    """
    environment, repository, before = _python_project(stack, retries=0)
    engine, _ = _python_engine(stack, tmp_path, [step])
    run = stack.admin.runs.start(environment, repository)
    _tick(stack)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(gone_at[0], gone_at[1], _vanish)
        with pytest.raises(_Died):
            _drain(stack, engine)
    lost = task_row(stack.db, run.id)
    assert (lost.status, lost.lease_owner, lost.attempt) == (TaskStatus.RUNNING, _WORKER, 1)
    assert (lost.max_attempts, lost.written_table_id) == (1, None)  # the last attempt, no stamp
    _expire(stack, lost.id)
    return environment, run.id, before


def _task_after(stack: QueryStack, run_id: str, asset: str = "silver.x") -> TaskRun:
    """Tick the scheduler once and read a task run.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run.
    asset
        The task's table.

    Returns
    -------
    TaskRun
        The task run, detached.
    """
    _tick(stack)
    return task_row(stack.db, run_id, asset)


def test_the_next_worker_sets_a_lost_python_assets_table_back_to_before_the_run(
    stack: QueryStack, tmp_path: Path
) -> None:
    """The process wrote [1, 2, 3] on its last attempt and its worker was lost.

    The scheduler does not fail the task: it puts it back ready for a set-back claim, uncounted,
    and says so in the run view. The next worker sets the table back to before the run and the
    task then ends failed with the lost message and the set-back's result.
    """
    environment, run_id, before = _lost_python_run(
        stack, tmp_path, _writes([1, 2, 3]), (worker_task_checks, "mark_written")
    )
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id != before  # unchecked
    waiting = _task_after(stack, run_id)
    assert (waiting.status, waiting.setting_back, waiting.attempt) == (
        TaskStatus.READY,
        True,
        1,
    )
    assert (waiting.set_back_claims, waiting.error_message) == (0, None)
    (shown,) = stack.admin.runs.get(environment, run_id).tasks
    assert shown.setting_back is True and shown.status == "ready"
    assert shown.waiting_for is not None
    assert (shown.waiting_for.code, shown.waiting_for.message) == (
        "setting_back",
        SETTING_BACK_MESSAGE,
    )

    _drain(stack, _engine(stack, "set-back-worker"))
    done = task_row(stack.db, run_id)
    assert (done.status, done.attempt, done.set_back_claims) == (TaskStatus.FAILED, 1, 1)
    assert done.error_message == set_back_ending(LOST_MESSAGE, SET_BACK_DONE)
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id == before
    assert _ids(stack, environment) == [7, 8]
    _tick(stack)
    assert stack.admin.runs.get(environment, run_id).status == "failed"
    assert stack.admin.checks.for_run(environment, run_id) == []  # no check was run


def test_a_lost_python_task_that_never_wrote_says_so_and_leaves_the_table(
    stack: QueryStack, tmp_path: Path
) -> None:
    """The worker was lost before the function wrote: there is nothing to set back."""
    environment, run_id, before = _lost_python_run(
        stack, tmp_path, _fails_for_good, (worker_tasks, "record_attempt")
    )
    waiting = _task_after(stack, run_id)
    assert (waiting.status, waiting.setting_back) == (TaskStatus.READY, True)

    _drain(stack, _engine(stack, "set-back-worker"))
    done = task_row(stack.db, run_id)
    assert done.status is TaskStatus.FAILED
    assert done.error_message == set_back_ending(LOST_MESSAGE, SET_BACK_NOTHING)
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id == before
    assert _ids(stack, environment) == [7, 8]
    assert stack.events(environment, AuditEventType.TABLE_ROLLED_BACK) == []


def test_a_cancel_while_a_stamped_tasks_worker_is_lost_sets_the_table_back_and_cancels(
    stack: QueryStack,
) -> None:
    """The worker wrote ids 1 to 3, was lost before its checks, and the user cancels the run.

    Before, the cancel ended the task in the same tick and the unchecked rows stayed. Now the
    task is not cancelled until the next worker has set the table back from its stamp.
    """
    task = _task(stack)
    assert task.written is not None
    assert stack.admin.runs.cancel(task.environment, str(task.run_id)).cancel_requested
    _expire(stack, task.id)
    _tick(stack)
    waiting = task_row(stack.db, str(task.run_id))
    assert (waiting.status, waiting.setting_back, waiting.attempt) == (TaskStatus.READY, True, 1)
    (_below, shown) = stack.admin.runs.get(task.environment, str(task.run_id)).tasks
    assert shown.waiting_for is not None and shown.waiting_for.code == "setting_back"

    _drain(stack, _engine(stack, "set-back-worker"))
    done = task_row(stack.db, str(task.run_id))
    assert done.status is TaskStatus.CANCELLED
    assert done.error_message == set_back_ending(RUN_CANCELLED_MESSAGE, SET_BACK_DONE)
    assert _snapshot(stack, task) == task.before
    assert _ids(stack, task.environment) == [7, 8]
    assert _outcomes(stack, task) == []  # no check ran: the claim only sets back
    assert _stamp(stack, task) is None  # the rollback that landed cleared it
    assert _rollbacks(stack, task) == [(task.claim + 1, "rolled_back")]
    _tick(stack)
    assert stack.admin.runs.get(task.environment, str(task.run_id)).status == "cancelled"


def test_a_memory_stop_after_a_write_hands_the_task_back_and_the_next_worker_sets_it_back(
    stack: QueryStack, tmp_path: Path
) -> None:
    """The process wrote [1, 2, 3] on its last attempt and the worker stopped for memory.

    The stop's record hands the task back, as a lost worker's reclaim does: ready, `setting_back`,
    no attempt counted, and the stop's message waiting in the row. The next worker sets the table
    back to before the run, and the task ends failed with the stop's message and the set-back's
    result.
    """
    from lakana_worker.memory import COMMITTED_BEFORE_STOPPED_MESSAGE, Limit, task_message

    environment, run_id, before = _lost_python_run(
        stack, tmp_path, _writes([1, 2, 3]), (worker_task_checks, "mark_written")
    )
    stopped = task_row(stack.db, run_id)
    message = task_message(Limit(4 * 1024**3, "4GB", "x"))
    with Session(stack.db) as session:
        assert record_attempt(
            session,
            stopped.id,
            _WORKER,
            stopped.claim_count,
            kind=FailureKind.PERMANENT,
            rand=lambda: 0.5,
            error_message=message,
            on_committed=COMMITTED_BEFORE_STOPPED_MESSAGE,
            stopped=True,
        )
    handed = task_row(stack.db, run_id)
    assert (handed.status, handed.setting_back, handed.attempt, handed.set_back_claims) == (
        TaskStatus.READY,
        True,
        1,
        0,
    )
    assert handed.error_message == message
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id != before  # unchecked

    _drain(stack, _engine(stack, "set-back-worker"))
    done = task_row(stack.db, run_id)
    assert (done.status, done.attempt, done.set_back_claims) == (TaskStatus.FAILED, 1, 1)
    assert done.error_message == set_back_ending(message, SET_BACK_DONE)
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id == before
    assert _ids(stack, environment) == [7, 8]
    _tick(stack)
    assert stack.admin.runs.get(environment, run_id).status == "failed"


def test_a_cancel_a_live_worker_sees_after_its_write_hands_the_task_back_to_be_set_back(
    stack: QueryStack,
) -> None:
    """The worker holds its claim, the user cancels, and the worker ends the attempt.

    Before, the worker ended the task cancelled and the unchecked rows (ids 1 to 3) stayed in the
    table. Now it hands the task back under its claim, and the next worker sets the table back
    from the stamp before the task ends cancelled.
    """
    task = _task(stack)
    assert task.written is not None
    assert stack.admin.runs.cancel(task.environment, str(task.run_id)).cancel_requested
    with Session(stack.db) as session:
        assert record_attempt(
            session,
            task.id,
            task.worker,
            task.claim,
            kind=FailureKind.CANCELLED,
            rand=lambda: 0.5,
            error_message=RUN_CANCELLED_MESSAGE,
        )
    handed = task_row(stack.db, str(task.run_id))
    assert (handed.status, handed.setting_back, handed.attempt, handed.set_back_claims) == (
        TaskStatus.READY,
        True,
        1,
        0,
    )
    assert _snapshot(stack, task) != task.before  # the unchecked rows are still in the table
    (_below, shown) = stack.admin.runs.get(task.environment, str(task.run_id)).tasks
    assert shown.waiting_for is not None and shown.waiting_for.code == "setting_back"

    _drain(stack, _engine(stack, "set-back-worker"))
    done = task_row(stack.db, str(task.run_id))
    assert done.status is TaskStatus.CANCELLED
    assert done.error_message == set_back_ending(RUN_CANCELLED_MESSAGE, SET_BACK_DONE)
    assert _snapshot(stack, task) == task.before
    assert _ids(stack, task.environment) == [7, 8]
    assert _outcomes(stack, task) == []  # no check ran: the claim only sets back
    _tick(stack)
    assert stack.admin.runs.get(task.environment, str(task.run_id)).status == "cancelled"


def test_a_cancel_after_the_lost_task_was_put_back_still_sets_the_table_back(
    stack: QueryStack,
) -> None:
    """The only worker wrote ids 1 to 3 and was lost; the user cancels once the task waits again.

    The reclaim put the task back to wait for its checks. A cancel of a waiting task used to end
    it at once and leave the unchecked rows; now the cancel sends it to set the table back, and
    the next worker does that before the task ends cancelled.
    """
    task = _task(stack)
    assert task.written is not None
    _expire(stack, task.id)
    _tick(stack)
    waiting = task_row(stack.db, str(task.run_id))
    assert (waiting.status, waiting.setting_back) == (TaskStatus.READY, False)  # for its checks
    assert stack.admin.runs.cancel(task.environment, str(task.run_id)).cancel_requested
    _tick(stack)
    back = task_row(stack.db, str(task.run_id))
    assert (back.status, back.setting_back, back.attempt, back.set_back_claims) == (
        TaskStatus.READY,
        True,
        1,
        0,
    )
    assert _snapshot(stack, task) != task.before  # the unchecked rows are still in the table

    _drain(stack, _engine(stack, "set-back-worker"))
    done = task_row(stack.db, str(task.run_id))
    assert done.status is TaskStatus.CANCELLED
    assert done.error_message == set_back_ending(RUN_CANCELLED_MESSAGE, SET_BACK_DONE)
    assert _snapshot(stack, task) == task.before
    assert _ids(stack, task.environment) == [7, 8]
    assert _outcomes(stack, task) == []  # no check ran: the claim only sets back
    _tick(stack)
    assert stack.admin.runs.get(task.environment, str(task.run_id)).status == "cancelled"


def _cancelled_and_lost(stack: QueryStack) -> _Task:
    """Make a stamped task of a cancelled run whose worker is lost, and reclaim it for set-back.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    _Task
        The task, ready with `setting_back` after one tick.
    """
    task = _task(stack)
    stack.admin.runs.cancel(task.environment, str(task.run_id))
    _expire(stack, task.id)
    _tick(stack)
    row = task_row(stack.db, str(task.run_id))
    assert (row.status, row.setting_back, row.set_back_claims) == (TaskStatus.READY, True, 0)
    return task


def _claim_and_lose(stack: QueryStack, task: _Task, worker: str) -> int:
    """Take the set-back claim of a task as a worker, which is then lost without doing anything.

    Parameters
    ----------
    stack
        The stack.
    task
        The task.
    worker
        The claiming worker's id.

    Returns
    -------
    int
        The claim counter the claim got.
    """
    with Session(stack.db) as session:
        claimed = claim_task(session, worker, _LEASE)
        assert claimed is not None and claimed.task_run_id == task.id and claimed.setting_back
    _expire(stack, task.id)
    _tick(stack)
    return claimed.claim


def test_a_set_back_claim_whose_worker_is_lost_too_is_followed_by_one_that_finishes_it(
    stack: QueryStack,
) -> None:
    """The first set-back claim vanishes; the scheduler offers the task again, uncounted."""
    task = _cancelled_and_lost(stack)
    lost_claim = _claim_and_lose(stack, task, "lost-set-back-worker")
    assert lost_claim == task.claim + 1
    again = task_row(stack.db, str(task.run_id))
    assert (again.status, again.setting_back, again.set_back_claims) == (
        TaskStatus.READY,
        True,
        1,
    )
    assert again.error_message is None and _snapshot(stack, task) != task.before

    _drain(stack, _engine(stack, "set-back-worker"))
    done = task_row(stack.db, str(task.run_id))
    assert (done.status, done.set_back_claims) == (TaskStatus.CANCELLED, 2)
    assert done.error_message == set_back_ending(RUN_CANCELLED_MESSAGE, SET_BACK_DONE)
    assert _snapshot(stack, task) == task.before
    assert _rollbacks(stack, task) == [(task.claim + 2, "rolled_back")]


def test_a_task_whose_set_back_claims_are_all_lost_ends_with_the_lost_set_back_message(
    stack: QueryStack,
) -> None:
    """Two set-back claims vanish: the task ends instead of looping, and the message says so."""
    task = _cancelled_and_lost(stack)
    _claim_and_lose(stack, task, "lost-set-back-worker-1")
    _claim_and_lose(stack, task, "lost-set-back-worker-2")
    done = task_row(stack.db, str(task.run_id))
    assert (done.status, done.set_back_claims) == (TaskStatus.CANCELLED, 2)
    assert done.error_message == SET_BACK_LOST
    assert _snapshot(stack, task) != task.before  # the table still holds the unchecked rows
    _tick(stack)
    assert stack.admin.runs.get(task.environment, str(task.run_id)).status == "cancelled"


def test_a_zombie_of_the_old_claim_is_refused_after_the_set_back_claim_was_taken(
    stack: QueryStack,
) -> None:
    """The old worker wakes up after its task was reclaimed and rolls back: refused.

    The set-back claim's own rollback, in a run asked to cancel, is the one commit the guard
    lets through, and it lands.
    """
    task = _cancelled_and_lost(stack)
    with Session(stack.db) as session:
        claimed = claim_task(session, "set-back-worker", _LEASE)
        assert claimed is not None and claimed.claim == task.claim + 1
    with Session(stack.db) as session, pytest.raises(PermissionDenied) as refused:
        _roll_back(session, task)  # the old claim
    assert str(refused.value) == SUPERSEDED_MESSAGE
    assert _snapshot(stack, task) != task.before and _rollbacks(stack, task) == []
    with Session(stack.db) as session:
        assert _roll_back(session, task, claim=claimed.claim) == Rollback(
            RollbackOutcome.ROLLED_BACK
        )
    assert _snapshot(stack, task) == task.before
    assert _rollbacks(stack, task) == [(claimed.claim, "rolled_back")]
