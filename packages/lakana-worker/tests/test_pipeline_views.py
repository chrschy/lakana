"""Views end to end in the worker."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.ipc
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    SecurableKind,
)
from lakana_core.catalog.query_service import QueryService, WriteTarget
from lakana_core.catalog.service import CatalogService
from lakana_core.config import QueryLimitsConfig, StorageConfig
from lakana_core.errors import ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    LineageEdge,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Table,
    WriteMode,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.executor import (
    CANCELLED_MESSAGE,
    DRAIN_MESSAGE,
    WORKER_STALE_MESSAGE,
    OutOfMemoryQuery,
    QueryCancelled,
    QueryCancelRequested,
    QueryTimeout,
    WorkerStaleQuery,
)
from lakana_worker.pipeline import _diagnose_views, _resolve_sources, run_one


@pytest.fixture
def session(file_session: Session) -> Session:
    """Run this module on a SQLite file, which the cancel flag's poller thread can read.

    Parameters
    ----------
    file_session
        The session on the file.

    Returns
    -------
    Session
        The same session.
    """
    return file_session


def _principal(session: Session, name: str) -> PrincipalRef:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    PrincipalRef
        A reference to it.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name=name))
    session.commit()
    return PrincipalRef(id=principal_id, name=name, type=PrincipalType.USER)


@pytest.fixture
def admin(session: Session) -> PrincipalRef:
    """Provide the setup principal.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        `admin`.
    """
    return _principal(session, "admin")


@pytest.fixture
def catalog(session: Session, admin: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` with `bronze.orders` (ids 1 to 3, two regions).

    Parameters
    ----------
    session
        Database session.
    admin
        The setup principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        An allow-all catalog.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        admin,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    orders = pa.table(
        {"id": pa.array([1, 2, 3], pa.int64()), "region": pa.array(["eu", "us", "eu"])}
    )
    catalog.write_batches(
        admin,
        TableRef.from_dotted("dev", "bronze.orders"),
        mode=WriteMode.CREATE,
        schema=orders.schema,
        batches=orders.to_batches(),
    )
    return catalog


def _view(
    session: Session,
    principal: PrincipalRef,
    dotted: str,
    sql: str,
    authz: AuthorizationService | None = None,
) -> QueryRun:
    """Create or replace a view through a `mode: view` run.

    Parameters
    ----------
    session
        Database session.
    principal
        The creator.
    dotted
        The view.
    sql
        Its SELECT.
    authz
        The worker's authorization; allow-all when `None`.

    Returns
    -------
    QueryRun
        The finished run.
    """
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal, "dev", sql, WriteTarget(table=dotted, mode=WriteMode.VIEW)
    )
    run_one(session, run.id, authz or AllowAllAuthorization())
    session.refresh(run)
    return run


def _query(
    session: Session, principal: PrincipalRef, tmp_path: Path, sql: str, *, granted: bool = True
) -> tuple[QueryRun, list[dict[str, object]]]:
    """Run a read query and return its rows.

    Parameters
    ----------
    session
        Database session.
    principal
        The reader.
    tmp_path
        The storage root, where results land.
    sql
        The query.
    granted
        Authorize the worker with real grants (`True`) or allow-all.

    Returns
    -------
    tuple
        The finished run and its rows (empty when it failed).
    """
    run = QueryService(session, AllowAllAuthorization()).submit_query(principal, "dev", sql)
    authz = GrantAuthorization(session) if granted else AllowAllAuthorization()
    run_one(session, run.id, authz)
    session.refresh(run)
    if run.result_location is None:
        return run, []
    with pyarrow.ipc.open_file(tmp_path / run.result_location) as reader:
        return run, reader.read_all().to_pylist()


def _grant_read(session: Session, admin: PrincipalRef, principal: str, dotted: str) -> None:
    """Grant READ_TABLE on one table or view.

    Parameters
    ----------
    session
        Database session.
    admin
        The granting principal.
    principal
        The grantee's name.
    dotted
        The table or view.
    """
    namespace, table = dotted.rsplit(".", 1)
    GrantService(session, AllowAllAuthorization()).grant(
        admin,
        principal=principal,
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace=namespace,
        table=table,
    )


def test_a_view_is_created_without_executing_and_read_through(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """`mode: view` plans and stores; a query through the view reads the base table."""
    run = _view(session, admin, "gold.v", "SELECT id, region FROM bronze.orders WHERE id > 1")

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    view = session.execute(select(Table).where(Table.name == "v")).scalar_one()
    assert run.target_table_id == view.id
    assert run.rows_written == 0
    assert view.view_definition is not None
    assert view.view_definition["columns"] == [
        {"name": "id", "type": "long", "nullable": True},
        {"name": "region", "type": "string", "nullable": True},
    ]
    edge = session.execute(select(LineageEdge)).scalar_one()
    assert (edge.source_name, edge.target_name, edge.query_run_id) == (
        "bronze.orders",
        "gold.v",
        run.id,
    )
    read, rows = _query(
        session, admin, tmp_path, "SELECT id FROM gold.v ORDER BY id", granted=False
    )
    assert read.status == QueryRunStatus.SUCCEEDED, read.error_message
    assert rows == [{"id": 2}, {"id": 3}]


def test_a_view_stored_with_an_arrow_lambda_runs_and_is_read_through(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A view written with `x -> …` (DuckDB 1.5 syntax) plans and reads on DuckDB 1.5 and 2.0."""
    run = _view(
        session,
        admin,
        "gold.v",
        "SELECT id, list_transform([id, id], x -> x * 10) AS tens FROM bronze.orders",
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    read, rows = _query(
        session, admin, tmp_path, "SELECT tens FROM gold.v ORDER BY id", granted=False
    )
    assert read.status == QueryRunStatus.SUCCEEDED, read.error_message
    assert rows == [{"tens": [10, 10]}, {"tens": [20, 20]}, {"tens": [30, 30]}]


def test_a_view_over_an_untyped_null_is_created_and_read_through(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A view stores no data, so `SELECT NULL AS x` is a valid view column on either DuckDB.

    DuckDB 1.5 types the `NULL` as an integer, DuckDB 2 as Arrow `null`; either way the view
    is created and reads back `None`.
    """
    run = _view(session, admin, "gold.v", "SELECT id, NULL AS x FROM bronze.orders")

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    read, rows = _query(session, admin, tmp_path, "SELECT x FROM gold.v ORDER BY id", granted=False)
    assert read.status == QueryRunStatus.SUCCEEDED, read.error_message
    assert rows == [{"x": None}] * 3


def test_creating_a_view_never_runs_its_query(
    session: Session, admin: PrincipalRef, catalog: CatalogService
) -> None:
    """Planning is `LIMIT 0`: a SELECT that would fail at runtime still becomes a view."""
    run = _view(session, admin, "gold.v", "SELECT error('boom') AS x FROM bronze.orders")

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message


def test_a_runtime_failure_through_a_healthy_view_keeps_its_own_error(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Review minor: diagnosis must not blame a healthy view for someone else's failure.

    The view from `test_creating_a_view_never_runs_its_query` plans fine on
    its own (`describe_query` never runs `error()`); only *executing* it
    fails. `_diagnose_views` re-plans it, finds nothing wrong, and returns
    `None`, so `run_one` reports the query's own (opaque, redacted) error —
    never a "no longer plans against its references" message that would
    misdirect the reader at a view that isn't the problem.
    """
    _view(session, admin, "gold.v", "SELECT error('boom') AS x FROM bronze.orders")

    read, _ = _query(session, admin, tmp_path, "SELECT x FROM gold.v", granted=False)

    assert read.status == QueryRunStatus.FAILED
    assert "no longer plans" not in (read.error_message or "")
    assert "boom" in (read.error_message or "")


def test_views_nest(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A view over a view expands recursively."""
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")
    _view(session, admin, "gold.w", "SELECT id FROM gold.v WHERE id < 3")

    read, rows = _query(
        session, admin, tmp_path, "SELECT id FROM gold.w ORDER BY id", granted=False
    )

    assert read.status == QueryRunStatus.SUCCEEDED, read.error_message
    assert rows == [{"id": 1}, {"id": 2}]


def test_replacing_a_view_to_close_a_cycle_is_refused(
    session: Session, admin: PrincipalRef, catalog: CatalogService
) -> None:
    """No cycle, even through a replacement."""
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")
    _view(session, admin, "gold.w", "SELECT id FROM gold.v")

    run = _view(session, admin, "gold.v", "SELECT id FROM gold.w")

    assert run.status == QueryRunStatus.FAILED
    assert "references itself" in (run.error_message or "")


def test_a_view_never_widens_access(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Security: READ on the view alone is not enough, and base policies still apply."""
    _view(session, admin, "gold.v", "SELECT id, region FROM bronze.orders")
    bob = _principal(session, "bob")
    _grant_read(session, admin, "bob", "gold.v")

    denied, _ = _query(session, bob, tmp_path, "SELECT id FROM gold.v")
    assert denied.status == QueryRunStatus.FAILED
    assert "PermissionDenied" in (denied.error_message or "")

    _grant_read(session, admin, "bob", "bronze.orders")
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        admin,
        principal="bob",
        environment="dev",
        namespace="bronze",
        table="orders",
        expression="id > 1",
    )
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        admin,
        principal="bob",
        environment="dev",
        namespace="bronze",
        table="orders",
        column="region",
        expression="'**'",
    )
    allowed, rows = _query(session, bob, tmp_path, "SELECT id, region FROM gold.v ORDER BY id")
    assert allowed.status == QueryRunStatus.SUCCEEDED, allowed.error_message
    assert rows == [{"id": 2, "region": "**"}, {"id": 3, "region": "**"}]


def test_creating_a_view_needs_read_on_what_it_references(
    session: Session, admin: PrincipalRef, catalog: CatalogService
) -> None:
    """The creator holds READ on everything referenced."""
    bob = _principal(session, "bob")
    GrantService(session, AllowAllAuthorization()).grant(
        admin,
        principal="bob",
        action=Action.CREATE_TABLE,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace="gold",
    )

    run = _view(session, bob, "gold.v", "SELECT id FROM bronze.orders", GrantAuthorization(session))

    assert run.status == QueryRunStatus.FAILED
    assert session.execute(select(Table).where(Table.name == "v")).first() is None


def test_a_view_over_a_dropped_table_fails_naming_the_view(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A stale view fails naming the view and the missing reference."""
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")
    catalog.drop_table(admin, TableRef.from_dotted("dev", "bronze.orders"))

    read, _ = _query(session, admin, tmp_path, "SELECT id FROM gold.v", granted=False)

    assert read.status == QueryRunStatus.FAILED
    assert "'gold.v' references 'bronze.orders'" in (read.error_message or "")


def test_a_view_whose_reference_lost_a_column_fails_naming_the_view(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A view that no longer plans is diagnosed and named."""
    _view(session, admin, "gold.v", "SELECT id, region FROM bronze.orders")
    narrowed = pa.table({"id": pa.array([1], pa.int64())})
    catalog.write_batches(
        admin,
        TableRef.from_dotted("dev", "bronze.orders"),
        mode=WriteMode.REPLACE,
        schema=narrowed.schema,
        batches=narrowed.to_batches(),
        evolve_schema=True,
    )

    read, _ = _query(session, admin, tmp_path, "SELECT id FROM gold.v", granted=False)

    assert read.status == QueryRunStatus.FAILED
    assert "View 'gold.v' no longer plans against its references (bronze.orders)" in (
        read.error_message or ""
    )


def test_a_broken_view_over_a_policy_bound_table_shows_the_fixed_message(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The diagnosis of a view quotes the engine; under a row filter the fixed message stands."""
    _view(session, admin, "gold.v", "SELECT id, region FROM bronze.orders")
    bob = _principal(session, "bob")
    _grant_read(session, admin, "bob", "gold.v")
    _grant_read(session, admin, "bob", "bronze.orders")
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        admin,
        principal="bob",
        environment="dev",
        namespace="bronze",
        table="orders",
        expression="id > 1",
    )
    narrowed = pa.table({"id": pa.array([1], pa.int64())})
    catalog.write_batches(
        admin,
        TableRef.from_dotted("dev", "bronze.orders"),
        mode=WriteMode.REPLACE,
        schema=narrowed.schema,
        batches=narrowed.to_batches(),
        evolve_schema=True,
    )

    read, _ = _query(session, bob, tmp_path, "SELECT id FROM gold.v")

    assert read.status == QueryRunStatus.FAILED
    assert read.error_message is not None
    assert read.error_message.startswith("The query failed on a value it could not convert")
    assert "no longer plans" not in read.error_message
    assert "Binder" not in read.error_message and "Catalog" not in read.error_message


def test_a_view_is_never_resolved_as_a_table(
    session: Session, admin: PrincipalRef, catalog: CatalogService
) -> None:
    """Defence in depth: an unexpanded view reaching resolution is refused, not read."""
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")

    with pytest.raises(ValidationError, match="is a view"):
        _resolve_sources(session, "dev", admin, ["gold.v"], AllowAllAuthorization())


def test_querying_an_unreadable_view_fails_with_the_same_opaque_message_as_a_table(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A view a reader has no grant on at all is denied like a denied table (no existence leak)."""
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")
    bob = _principal(session, "bob")

    denied, _ = _query(session, bob, tmp_path, "SELECT id FROM gold.v")

    assert denied.status == QueryRunStatus.FAILED
    assert denied.error_message == (
        "Resolving the tables this query reads failed (PermissionDenied)."
    )


def test_diagnose_views_skips_a_view_that_vanished_before_diagnosis(
    session: Session, admin: PrincipalRef, catalog: CatalogService
) -> None:
    """A view already gone by diagnosis time is skipped, not treated as the culprit.

    Diagnosis runs strictly after the query it explains has already failed,
    so a view it once expanded could in principle be dropped by another
    session in between; `find_view` returning `None` for it then must not
    be read as "this view is the problem" — there is no `ViewDefinition` to
    build that message from.
    """
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")

    with mock.patch.object(CatalogService, "find_view", return_value=None):
        result = _diagnose_views(
            session,
            "dev",
            admin,
            ("gold.v",),
            AllowAllAuthorization(),
            None,
            QueryLimitsConfig(),
            [],
            spill=False,
        )

    assert result is None


_STOPS = [
    pytest.param(
        QueryCancelRequested(CANCELLED_MESSAGE), QueryRunStatus.CANCELLED, "cancelled", id="cancel"
    ),
    pytest.param(
        QueryCancelled(CANCELLED_MESSAGE), QueryRunStatus.FAILED, "cancelled", id="task-cancel"
    ),
    pytest.param(QueryCancelled(DRAIN_MESSAGE), QueryRunStatus.FAILED, "cancelled", id="drain"),
    pytest.param(
        QueryTimeout("Query exceeded the time limit (1 s). Aggregate or filter earlier."),
        QueryRunStatus.FAILED,
        "timeout",
        id="timeout",
    ),
    pytest.param(
        OutOfMemoryQuery("Query failed: out of memory. Read less."),
        QueryRunStatus.FAILED,
        "out_of_memory",
        id="memory",
    ),
    pytest.param(
        WorkerStaleQuery(WORKER_STALE_MESSAGE),
        QueryRunStatus.FAILED,
        "worker_lost",
        id="stale",
    ),
]


@pytest.mark.parametrize(("stop", "status", "cause"), _STOPS)
def test_a_stopped_query_over_a_view_keeps_its_own_message(
    session: Session,
    admin: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: ValidationError,
    status: QueryRunStatus,
    cause: str,
) -> None:
    """A stopped query is reported as stopped, never as a broken view, and no describe starts.

    A diagnosis that did run would start the describe, which fails here, and so name the view.
    """
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")

    def stopped(*args: object, **kwargs: object) -> pa.Table:
        raise stop

    def no_describe(*args: object, **kwargs: object) -> pa.Schema:
        raise AssertionError("a describe child was started")

    monkeypatch.setattr("lakana_worker.pipeline.execute_query", stopped)
    monkeypatch.setattr("lakana_worker.pipeline.describe_query", no_describe)
    monkeypatch.setattr(
        "lakana_worker.pipeline._diagnose_views",
        lambda *args, **kwargs: pytest.fail("the views were diagnosed"),
    )

    run, _ = _query(session, admin, tmp_path, "SELECT id FROM gold.v", granted=False)

    assert (run.status, run.error_cause, run.error_message) == (status, cause, str(stop))


@pytest.mark.parametrize(
    "stop",
    [
        QueryCancelled(DRAIN_MESSAGE),
        QueryTimeout("too slow"),
        WorkerStaleQuery(WORKER_STALE_MESSAGE),
    ],
)
def test_a_diagnosis_that_is_stopped_itself_leaves_the_failure_of_the_query(
    session: Session,
    admin: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
    stop: ValidationError,
) -> None:
    """A cancel or the drain that arrives during the diagnosis says nothing about the view."""
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")

    def stopped(*args: object, **kwargs: object) -> pa.Schema:
        raise stop

    monkeypatch.setattr("lakana_worker.pipeline.describe_query", stopped)
    result = _diagnose_views(
        session,
        "dev",
        admin,
        ("gold.v",),
        AllowAllAuthorization(),
        None,
        QueryLimitsConfig(),
        [],
        spill=False,
    )

    assert result is None


@pytest.mark.parametrize("placeholder", ["$1", "?"])
def test_a_stored_view_with_a_placeholder_fails_without_binding_anything(
    session: Session, admin: PrincipalRef, catalog: CatalogService, tmp_path: Path, placeholder: str
) -> None:
    """The named-parameter rule reaches stored view SQL: a placeholder there never binds a URL list.

    Creation refuses a placeholder, so the stored SQL is tampered with
    directly — the shape of a row predating the fix. The read fails with the
    fixed placeholder message and no file path.
    """
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")
    view = session.execute(select(Table).where(Table.name == "v")).scalar_one()
    view.view_definition = {
        **(view.view_definition or {}),
        "sql": f"SELECT id, {placeholder} AS leaked FROM bronze.orders",
    }
    session.commit()

    read, rows = _query(session, admin, tmp_path, "SELECT * FROM gold.v", granted=False)

    assert read.status == QueryRunStatus.FAILED
    assert rows == []
    assert "placeholders are not allowed" in (read.error_message or "")
    assert str(tmp_path) not in (read.error_message or "")


def test_a_view_run_is_fenced_and_leaves_its_evidence(
    session: Session,
    admin: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A view counts as a write: committed, it is on its run before the status is stamped."""
    import lakana_worker.pipeline as pipeline_module

    run = QueryService(session, AllowAllAuthorization()).submit_write(
        admin,
        "dev",
        "SELECT id FROM bronze.orders",
        WriteTarget(table="gold.fv", mode=WriteMode.VIEW),
    )
    committed: list[tuple[object, ...]] = []
    real = pipeline_module._keep_a_prior_stamp
    engine = session.get_bind()

    def read_then_stamp(
        stamp_session: Session,
        stamped: QueryRun,
        outcome: QueryRunStatus,
        *,
        worker_id: str | None = None,
    ) -> bool:
        with Session(engine) as other:
            row = other.execute(
                select(QueryRun.status, QueryRun.rows_written, QueryRun.snapshot_id).where(
                    QueryRun.id == run.id
                )
            ).one()
            committed.append(tuple(row))
        return real(stamp_session, stamped, outcome, worker_id=worker_id)

    monkeypatch.setattr(pipeline_module, "_keep_a_prior_stamp", read_then_stamp)
    assert run_one(session, run.id, AllowAllAuthorization(), worker_id="w1") is None
    assert committed == [(QueryRunStatus.RUNNING, 0, None)]
    ended = session.get(QueryRun, run.id)
    view = session.execute(select(Table).where(Table.name == "fv")).scalar_one()
    assert ended is not None and ended.target_table_id == view.id


def test_a_write_over_a_view_that_the_fence_refuses_is_not_diagnosed_as_a_broken_view(
    session: Session,
    admin: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3.5: the refusal is a `PermissionDenied`, not a `ValidationError`, so no describe starts."""
    from lakana_core.runs.lockdown import WRITE_FENCE_MESSAGE

    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        admin, "dev", "SELECT id FROM gold.v", WriteTarget(table="gold.out", mode=WriteMode.CREATE)
    )
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run.id)
        .values(status=QueryRunStatus.QUEUED, worker_id=None)
    )
    session.commit()
    engine = session.get_bind()
    import lakana_worker.pipeline as pipeline_module

    real = pipeline_module.stream_query

    def ended_meanwhile(
        sql: str,
        parameters: Mapping[str, object] | None = None,
        *,
        read_prefixes: Sequence[str] = (),
        spill: bool = False,
        limits: QueryLimitsConfig | None = None,
    ) -> AbstractContextManager[tuple[pa.Schema, Iterator[pa.RecordBatch]]]:
        with Session(engine) as other:
            other.execute(
                update(QueryRun)
                .where(QueryRun.id == run.id)
                .values(status=QueryRunStatus.FAILED, error_message="ended")
            )
            other.commit()
        return real(sql, parameters, read_prefixes=read_prefixes, spill=spill, limits=limits)

    monkeypatch.setattr(pipeline_module, "stream_query", ended_meanwhile)
    monkeypatch.setattr(
        pipeline_module, "_diagnose_views", lambda *a, **k: pytest.fail("the views were diagnosed")
    )
    failure = run_one(session, run.id, AllowAllAuthorization(), worker_id="w1")
    assert failure is not None and str(failure) == WRITE_FENCE_MESSAGE
    assert pipeline_module.failure_cause(failure).value == "worker_lost"
