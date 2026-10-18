"""A succeeded query adds a read to each table it names, in the transaction that completes it."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_pipeline import _write_one_data_file_and_commit

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    Principal,
    PrincipalType,
    QueryRunStatus,
    Table,
    TableRead,
)
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.pipeline import run_one


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, persisted.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The principal.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="ada"))
    session.commit()
    return PrincipalRef(id=principal_id, name="ada", type=PrincipalType.USER)


@pytest.fixture
def sales(session: Session, principal: PrincipalRef, tmp_path: Path) -> QueryService:
    """Create `dev` with `gold.sales` (two rows) and return a query service for it.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        Storage root.

    Returns
    -------
    QueryService
        An allow-all query service.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "region", StringType(), required=False),
        ),
    )
    arrow = pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())])
    _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table({"id": [1, 2], "region": ["eu", "us"]}, schema=arrow),
    )
    return QueryService(session, AllowAllAuthorization())


def _counts(session: Session) -> list[tuple[str, str, int]]:
    """List the stored reads as table name, principal name and count.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of tuple
        The rows, by table name.
    """
    session.expire_all()
    return [
        (name, who, count)
        for name, who, count in session.execute(
            select(Table.name, Principal.name, TableRead.read_count)
            .join(Table, Table.id == TableRead.table_id)
            .join(Principal, Principal.id == TableRead.principal_id)
            .order_by(Table.name)
        )
    ]


def test_a_succeeded_query_counts_a_read_per_day_and_principal(
    session: Session, principal: PrincipalRef, sales: QueryService
) -> None:
    for _ in range(2):
        run = sales.submit_query(
            principal, "dev", "SELECT id FROM gold.sales s JOIN gold.sales t USING (id)"
        )
        run_one(session, run.id, AllowAllAuthorization())
        session.refresh(run)
        assert run.status is QueryRunStatus.SUCCEEDED, run.error_message
    assert _counts(session) == [("sales", "ada", 2)]
    (row,) = session.scalars(select(TableRead)).all()
    assert row.day == datetime.now(UTC).date() or row.day == row.last_read_at.date()
    assert abs(row.last_read_at - datetime.now(UTC)).total_seconds() < 300


def test_a_failed_query_counts_nothing(
    session: Session, principal: PrincipalRef, sales: QueryService
) -> None:
    run = sales.submit_query(principal, "dev", "SELECT nope FROM gold.sales")
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    assert run.status is QueryRunStatus.FAILED
    assert _counts(session) == []


def test_a_query_of_a_table_that_does_not_exist_counts_nothing(
    session: Session, principal: PrincipalRef, sales: QueryService
) -> None:
    run = sales.submit_query(principal, "dev", "SELECT 1 FROM gold.missing")
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    assert run.status is QueryRunStatus.FAILED
    assert _counts(session) == []


def test_a_query_that_names_no_table_counts_nothing(
    session: Session, principal: PrincipalRef, sales: QueryService
) -> None:
    run = sales.submit_query(principal, "dev", "SELECT 1 AS one")
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    assert run.status is QueryRunStatus.SUCCEEDED, run.error_message
    assert _counts(session) == []
