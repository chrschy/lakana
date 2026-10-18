"""`describe_table`: one read path for managed tables, external tables and views."""

from __future__ import annotations

import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import update
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import PermissionDenied
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Table, WriteMode
from lakana_core.storage.resolver import StorageResolver


class _DenySecurable:
    """Authorizes everything except one specific securable's `READ_TABLE`.

    Parameters
    ----------
    denied
        The one securable `authorize`/`permits` refuse.
    """

    def __init__(self, denied: Securable) -> None:
        self._denied = denied

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse `READ_TABLE` on the denied securable; permit everything else.

        Parameters
        ----------
        principal
            The acting principal (unused; this stub denies by securable
            alone, not by identity).
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            If `securable` is the denied one and `action` is `READ_TABLE`.
        """
        if action is Action.READ_TABLE and securable == self._denied:
            raise PermissionDenied("denied")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, as a boolean.

        Parameters
        ----------
        principal
            The acting principal.
        action
            The action requested.
        securable
            The object acted upon.

        Returns
        -------
        bool
            `False` if `authorize` would raise, else `True`.
        """
        try:
            self.authorize(principal, action, securable)
        except PermissionDenied:
            return False
        return True


def test_describe_table_covers_every_kind(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A managed table has metadata; a view has SQL and references; an external has neither."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([1], pa.int64())})
    orders = TableRef.from_dotted("dev", "bronze.orders")
    catalog.write_batches(
        principal, orders, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
    )
    view = TableRef.from_dotted("dev", "gold.v")
    catalog.create_or_replace_view(
        principal,
        view,
        sql="SELECT id FROM bronze.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    external = TableRef.from_dotted("dev", "raw.events")
    catalog.create_external_table(
        principal, external, location_pattern="raw/events/*.parquet", format="parquet"
    )

    managed = catalog.describe_table(principal, orders)
    described_view = catalog.describe_table(principal, view)
    described_external = catalog.describe_table(principal, external)

    assert (managed.kind, managed.sql, managed.references) == ("TABLE", None, ())
    assert managed.metadata_location is not None
    assert [c.name for c in managed.columns] == ["id"]
    assert described_view.kind == "VIEW"
    assert described_view.metadata_location is None
    assert described_view.sql == "SELECT id FROM bronze.orders"
    assert described_view.references == ((str(managed.id), "bronze.orders"),)
    assert described_view.columns == [ColumnSpec(name="id", type="long", nullable=True)]
    assert described_view.hidden_references == 0
    assert described_external.kind == "EXTERNAL"
    assert described_external.metadata_location is None
    assert [c.name for c in described_external.columns] == ["id"]
    assert (managed.file_format, described_view.file_format) == (None, None)
    assert described_external.file_format == "parquet"


def test_describe_table_hides_a_views_sql_from_a_reader_missing_a_reference(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """READ on the view alone must not disclose a reference the reader can't READ.

    A `gold.v` reader without READ on `bronze.orders` gets no SQL and no
    references — just a count — even though the view's row still names
    `bronze.orders`.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([1], pa.int64())})
    orders = TableRef.from_dotted("dev", "bronze.orders")
    catalog.write_batches(
        principal, orders, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
    )
    view = TableRef.from_dotted("dev", "gold.v")
    catalog.create_or_replace_view(
        principal,
        view,
        sql="SELECT id FROM bronze.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    denied = Securable.table_scope("dev", NamespacePath.from_parts(["bronze"]), "orders")
    reader = CatalogService(session, StorageResolver(session), _DenySecurable(denied))

    described = reader.describe_table(principal, view)

    assert described.kind == "VIEW"
    assert described.sql is None
    assert described.references == ()
    assert described.hidden_references == 1
    assert [c.name for c in described.columns] == ["id"]


def test_describe_table_counts_a_dropped_reference_as_hidden(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A reference dropped since the view was created can't be read, so it is withheld."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([1], pa.int64())})
    orders = TableRef.from_dotted("dev", "bronze.orders")
    catalog.write_batches(
        principal, orders, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
    )
    view = TableRef.from_dotted("dev", "gold.v")
    catalog.create_or_replace_view(
        principal,
        view,
        sql="SELECT id FROM bronze.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    catalog.drop_table(principal, orders)

    described = catalog.describe_table(principal, view)

    assert (described.sql, described.references, described.hidden_references) == (None, (), 1)


def _catalog_with_orders(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> CatalogService:
    """Build a catalog on `dev` whose `bronze.orders` has been created empty.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        Directory backing the environment's storage.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([], pa.int64())})
    catalog.write_batches(
        principal,
        TableRef.from_dotted("dev", "bronze.orders"),
        mode=WriteMode.CREATE,
        schema=data.schema,
        batches=data.to_batches(),
    )
    return catalog


def test_a_table_that_is_not_published_is_described_with_its_state(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog_with_orders(session, principal, tmp_path)
    ref = TableRef.from_dotted("dev", "bronze.orders")
    run_id = uuid.uuid4()
    session.execute(update(Table).where(Table.name == "orders").values(unpublished_run_id=run_id))
    session.commit()

    described = catalog.describe_table(principal, ref)

    assert described.state == "not_published"
    assert described.state_message == (
        "`bronze.orders` has not been published: its first build did not finish, "
        f"or failed a check (run {run_id}). It is published by the next run that builds it "
        "and passes its checks."
    )
    assert described.snapshot_id is None
    assert [c.name for c in described.columns] == ["id"]


def test_a_published_table_has_no_message(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog_with_orders(session, principal, tmp_path)

    described = catalog.describe_table(principal, TableRef.from_dotted("dev", "bronze.orders"))

    assert (described.state, described.state_message) == ("published", None)


def test_a_view_has_the_default_state(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog_with_orders(session, principal, tmp_path)
    view = TableRef.from_dotted("dev", "gold.v")
    catalog.create_or_replace_view(
        principal,
        view,
        sql="SELECT id FROM bronze.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )

    described = catalog.describe_table(principal, view)

    assert (described.state, described.state_message) == ("published", None)


def test_a_kept_table_is_described_with_its_state(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog_with_orders(session, principal, tmp_path)
    ref = TableRef.from_dotted("dev", "bronze.orders")
    run_id = uuid.uuid4()
    session.execute(
        update(Table)
        .where(Table.name == "orders")
        .values(kept_run_id=run_id, kept_task_run_id=uuid.uuid4(), kept_check="bronze.orders/a")
    )
    session.commit()

    described = catalog.describe_table(principal, ref)

    assert described.state == "kept"
    assert described.state_message == (
        f"Kept after a failed check (`bronze.orders/a`, run {run_id}): the table holds "
        "the data that failed. Tasks do not read it until its checks pass."
    )
    assert [c.name for c in described.columns] == ["id"]


def test_a_table_that_is_not_published_stays_not_published_with_a_note(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog_with_orders(session, principal, tmp_path)
    run_id = uuid.uuid4()
    session.execute(
        update(Table)
        .where(Table.name == "orders")
        .values(
            unpublished_run_id=run_id,
            kept_run_id=uuid.uuid4(),
            kept_task_run_id=uuid.uuid4(),
            kept_check="bronze.orders/a",
        )
    )
    session.commit()

    described = catalog.describe_table(principal, TableRef.from_dotted("dev", "bronze.orders"))

    assert described.state == "not_published"
    assert described.state_message == (
        "`bronze.orders` has not been published: its first build did not finish, "
        f"or failed a check (run {run_id}). It is published by the next run that builds it "
        "and passes its checks."
    )
