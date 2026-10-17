"""The query engine's error text is replaced on a read under a row filter or column mask.

A policy expression runs on every stored value, so when it fails the engine's text quotes a
value the policy hides. These tests run real DuckDB with real policy resolution and pin that a
failure of the kinds that quote a value is recorded as one fixed message, with and without a
read through a field-id map, and that a query with no policy keeps the engine's own text.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pyarrow as pa
import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ColumnMask,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    RowFilter,
    WriteMode,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.read_errors import policy_read_failure
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.pipeline import run_one

_SECRET = "HIDDEN-X"
_BIG = 2_000_000_000


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, with a persisted row.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The administrator, named ``admin``.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    return PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide a catalog with a `dev` environment rooted in `tmp_path`, with two tables.

    `gold.people` holds one row (`secret` text, `num` 2 000 000 000, `code` text);
    `gold.plain` holds the same row.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The environment's storage root.

    Returns
    -------
    CatalogService
        The catalog; it allows everything.
    """
    service = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    service.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table(
        {
            "id": pa.array([1], pa.int64()),
            "secret": pa.array([_SECRET], pa.string()),
            "num": pa.array([_BIG], pa.int32()),
            "code": pa.array([_SECRET], pa.string()),
        }
    )
    for name in ("people", "plain"):
        service.write_batches(
            principal,
            TableRef(
                environment="dev",
                namespace=NamespacePath.from_parts(["gold"]),
                name=name,
            ),
            mode=WriteMode.CREATE,
            schema=data.schema,
            batches=data.to_batches(),
        )
    return service


def _mask(
    session: Session, principal: PrincipalRef, catalog: CatalogService, column: str, sql: str
) -> None:
    """Put a column mask on `gold.people` for ``admin``, with any expression.

    The service refuses the expressions these tests need (a cast, a product), so the mask is
    created with ``NULL`` and the stored expression is then replaced: a mask created by an
    earlier version, or edited in the database, can hold any text.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    catalog
        The catalog.
    column
        The masked column.
    sql
        The mask expression, stored as it is.
    """
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="people",
        column=column,
        expression="NULL",
    )
    session.execute(update(ColumnMask).values(expression=sql))
    session.commit()


def _filter(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    table: str,
    sql: str,
    *,
    stored: bool = True,
) -> None:
    """Put a row filter on a table of `gold` for ``admin``.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    catalog
        The catalog.
    table
        The table's name.
    sql
        The filter expression.
    stored
        Create the filter with ``id > 0`` and then replace the expression, as `_mask` does;
        `False` passes `sql` through the service's own validation.
    """
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table=table,
        expression="id > 0" if stored else sql,
    )
    if stored:
        session.execute(update(RowFilter).where(RowFilter.table == table).values(expression=sql))
        session.commit()


def _query(session: Session, principal: PrincipalRef, sql: str) -> QueryRun:
    """Submit a query and run it in the worker.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    sql
        The query.

    Returns
    -------
    QueryRun
        The finished run, refreshed.
    """
    run = QueryService(session, AllowAllAuthorization()).submit_query(principal, "dev", sql)
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    return run


def _assert_fixed(run: QueryRun, table: str | None, output: str) -> None:
    """Assert that a run failed with the fixed message and shows no stored value anywhere.

    Parameters
    ----------
    run
        The finished run.
    table
        The dotted table the message names, or `None` for the variant without one.
    output
        Everything captured from the process's output streams and its log.
    """
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == policy_read_failure(table)
    assert run.error_cause == "invalid_query"
    for text in (run.error_message, output):
        assert _SECRET not in text
        assert str(_BIG) not in text


_MASKS = [
    ("secret", "strptime(\"secret\", '%Y')"),
    ("num", '"num" * 10'),
    ("secret", 'CAST("secret" AS INTEGER)'),
]


@pytest.mark.parametrize(("column", "expression"), _MASKS)
def test_a_failing_column_mask_shows_the_fixed_message(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    column: str,
    expression: str,
) -> None:
    """The failure of a mask on a stored value names the table and not the value."""
    caplog.set_level(logging.DEBUG)
    _mask(session, principal, catalog, column, expression)

    run = _query(session, principal, "SELECT * FROM gold.people")

    captured = capfd.readouterr()
    _assert_fixed(run, "gold.people", captured.out + captured.err + caplog.text)


def test_a_failing_row_filter_shows_the_fixed_message(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cast in a row filter fails on a stored value the same way."""
    caplog.set_level(logging.DEBUG)
    _filter(session, principal, catalog, "people", "CAST(code AS INTEGER) > 0")

    run = _query(session, principal, "SELECT id FROM gold.people")

    captured = capfd.readouterr()
    _assert_fixed(run, "gold.people", captured.out + captured.err + caplog.text)


def test_a_row_filter_the_service_accepts_can_fail_on_a_stored_value(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Comparing a text column with a number converts the text, which fails on the value."""
    _filter(session, principal, catalog, "people", "code = num", stored=False)

    run = _query(session, principal, "SELECT id FROM gold.people")

    _assert_fixed(run, "gold.people", "")


def test_two_tables_under_a_policy_use_the_message_without_a_table(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """With more than one policy-bound table the message cannot name the failing one."""
    _mask(session, principal, catalog, "secret", 'CAST("secret" AS INTEGER)')
    _filter(session, principal, catalog, "plain", "id > 0", stored=False)

    run = _query(
        session,
        principal,
        "SELECT p.secret FROM gold.people p JOIN gold.plain q ON p.id = q.id",
    )

    _assert_fixed(run, None, "")
    assert "on one of the tables" in (run.error_message or "")


def test_a_cast_of_the_readers_own_query_under_a_policy_shows_the_fixed_message(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The reader's cast of a value the policy hides is told the same way."""
    _filter(session, principal, catalog, "people", "id > 0", stored=False)

    run = _query(session, principal, "SELECT CAST(code AS INTEGER) FROM gold.people")

    _assert_fixed(run, "gold.people", "")


def test_a_conversion_error_on_a_table_without_a_policy_keeps_the_engine_text(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The data is the reader's own: the text stays as it was."""
    _mask(session, principal, catalog, "secret", "NULL")

    run = _query(session, principal, "SELECT CAST(code AS INTEGER) FROM gold.plain")

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith("Query failed: a value could not be converted")


def test_a_binder_error_under_a_policy_keeps_its_message(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Only the kinds that quote a value are replaced."""
    _mask(session, principal, catalog, "secret", "NULL")

    run = _query(session, principal, "SELECT nope FROM gold.people")

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith("Query failed: Binder Error")
    assert "The query failed on a value" not in run.error_message
