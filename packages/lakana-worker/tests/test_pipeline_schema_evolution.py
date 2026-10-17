"""Reads of a table whose schema changed between its data files, written with pyiceberg.

Every Iceberg table is read by field id, so the five schema changes Iceberg allows on a
top-level column read as the current schema says, under no policy, a row filter and a column
mask alike. A field inside a nested column is read by name, so a change to one fails closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.ipc
import pytest
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.table.update.schema import UpdateSchema
from pyiceberg.types import (
    DecimalType,
    DoubleType,
    FloatType,
    IcebergType,
    IntegerType,
    ListType,
    LongType,
    MapType,
    NestedField,
    StringType,
    StructType,
    TimestamptzType,
)
from sqlalchemy.orm import Session

import lakana_worker.pipeline as pipeline_module
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.field_ids import duckdb_type_of
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.config import StorageConfig
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Principal, PrincipalType, QueryRun, QueryRunStatus
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.pipeline import run_one


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
    """Provide a catalog with a `dev` environment rooted in `tmp_path`.

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
    return service


def _ref(name: str) -> TableRef:
    """Name a table of `gold`.

    Parameters
    ----------
    name
        The table's name.

    Returns
    -------
    TableRef
        The reference in ``dev``.
    """
    return TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name=name)


def _optional(field_id: int, name: str, field_type: IcebergType) -> NestedField:
    """Build an optional column.

    Parameters
    ----------
    field_id
        Its field id.
    name
        Its name.
    field_type
        Its type.

    Returns
    -------
    NestedField
        The column.
    """
    return NestedField(field_id, name, field_type, required=False)


def _append(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    table: LoadedTable,
    data: pa.Table,
) -> LoadedTable:
    """Append rows with pyiceberg and commit them through the catalog, as an Iceberg client does.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    ref
        The table.
    table
        The table as it is now.
    data
        Rows that fit the table's current schema.

    Returns
    -------
    LoadedTable
        The table after the commit.
    """
    transaction = StaticTable.from_metadata(table.metadata_location).transaction()
    transaction.append(data)
    return catalog.commit_table(
        principal, ref, requirements=transaction._requirements, updates=transaction._updates
    )


def _evolve(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    table: LoadedTable,
    change: Callable[[UpdateSchema], object],
) -> LoadedTable:
    """Change the table's schema with pyiceberg and commit it through the catalog.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    ref
        The table.
    table
        The table as it is now.
    change
        What to do to the schema.

    Returns
    -------
    LoadedTable
        The table after the commit.
    """
    transaction = StaticTable.from_metadata(table.metadata_location).transaction()
    with transaction.update_schema() as update:
        change(update)
    return catalog.commit_table(
        principal, ref, requirements=transaction._requirements, updates=transaction._updates
    )


def _created(
    catalog: CatalogService, principal: PrincipalRef, name: str, third: NestedField, rows: pa.Table
) -> tuple[TableRef, LoadedTable]:
    """Create `gold.<name>` with `id`, `region` and one more column, holding two rows.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    name
        The table's name.
    third
        The column the test changes, field id 3.
    rows
        Ids 1 and 2, regions EU and US, and the third column.

    Returns
    -------
    tuple of TableRef and LoadedTable
        The table and its state after the first append.
    """
    ref = _ref(name)
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(_optional(1, "id", LongType()), _optional(2, "region", StringType()), third),
    )
    return ref, _append(catalog, principal, ref, created, rows)


def _rows(ids: list[int], **columns: pa.Array) -> pa.Table:
    """Build rows with ids, alternating regions and the given columns.

    Parameters
    ----------
    ids
        The ids.
    **columns
        Further columns, in order.

    Returns
    -------
    pa.Table
        The rows.
    """
    regions = ["EU" if index % 2 == 0 else "US" for index in range(len(ids))]
    return pa.table({"id": pa.array(ids, pa.int64()), "region": regions, **columns})


def _added(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, list[object]]:
    """Add a column after the first file; it reads NULL there.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The changed column and its values for ids 1 to 4.
    """
    ref = _ref("added")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(_optional(1, "id", LongType()), _optional(2, "region", StringType())),
    )
    table = _append(catalog, principal, ref, created, _rows([1, 2]))
    table = _evolve(catalog, principal, ref, table, lambda u: u.add_column("tier", StringType()))
    _append(catalog, principal, ref, table, _rows([3, 4], tier=pa.array(["gold", "silver"])))
    return "tier", [None, None, "gold", "silver"]


def _renamed(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, list[object]]:
    """Rename a column after the first file; it keeps its data under the new name.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The changed column and its values for ids 1 to 4.
    """
    third = _optional(3, "label", StringType())
    ref, table = _created(
        catalog, principal, "renamed", third, _rows([1, 2], label=pa.array(["a", "b"]))
    )
    table = _evolve(catalog, principal, ref, table, lambda u: u.rename_column("label", "name"))
    _append(catalog, principal, ref, table, _rows([3, 4], name=pa.array(["c", "d"])))
    return "name", ["a", "b", "c", "d"]


def _readded(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, list[object]]:
    """Drop a column and add it again; it is a new column, NULL in the old file.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The changed column and its values for ids 1 to 4.
    """
    third = _optional(3, "tier", StringType())
    rows = _rows([1, 2], tier=pa.array(["x-old", "y-old"]))
    ref, table = _created(catalog, principal, "dropped_and_added_again", third, rows)
    table = _evolve(catalog, principal, ref, table, lambda u: u.delete_column("tier"))
    table = _evolve(catalog, principal, ref, table, lambda u: u.add_column("tier", LongType()))
    _append(catalog, principal, ref, table, _rows([3, 4], tier=pa.array([30, 40], pa.int64())))
    return "tier", [None, None, 30, 40]


def _int_to_long(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, list[object]]:
    """Widen an int column to long; it reads as long in both files.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The changed column and its values for ids 1 to 4.
    """
    third = _optional(3, "qty", IntegerType())
    ref, table = _created(
        catalog, principal, "int_to_long", third, _rows([1, 2], qty=pa.array([5, 6], pa.int32()))
    )
    table = _evolve(catalog, principal, ref, table, lambda u: u.update_column("qty", LongType()))
    _append(catalog, principal, ref, table, _rows([3, 4], qty=pa.array([7, 8], pa.int64())))
    return "qty", [5, 6, 7, 8]


def _float_to_double(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, list[object]]:
    """Widen a float column to double; it reads as double in both files.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The changed column and its values for ids 1 to 4.
    """
    third = _optional(3, "price", FloatType())
    rows = _rows([1, 2], price=pa.array([1.5, 2.5], pa.float32()))
    ref, table = _created(catalog, principal, "float_to_double", third, rows)
    table = _evolve(
        catalog, principal, ref, table, lambda u: u.update_column("price", DoubleType())
    )
    _append(catalog, principal, ref, table, _rows([3, 4], price=pa.array([3.5, 4.5], pa.float64())))
    return "price", [1.5, 2.5, 3.5, 4.5]


_CHANGES = {
    "added": _added,
    "renamed": _renamed,
    "dropped_and_added_again": _readded,
    "int_to_long": _int_to_long,
    "float_to_double": _float_to_double,
}
_WIDENED = {"int_to_long": pa.int64(), "float_to_double": pa.float64()}


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


def _result(tmp_path: Path, run: QueryRun) -> pa.Table:
    """Read the rows a successful run wrote.

    Parameters
    ----------
    tmp_path
        The environment's storage root.
    run
        The run, which must have succeeded.

    Returns
    -------
    pa.Table
        The result.
    """
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.result_location is not None
    with pyarrow.ipc.open_file(tmp_path / run.result_location) as reader:
        return reader.read_all()


@pytest.mark.parametrize("order", ["listed", "reversed"])
@pytest.mark.parametrize("policy", ["none", "row_filter", "column_mask"])
@pytest.mark.parametrize("change", sorted(_CHANGES))
def test_a_read_follows_the_schema_change_by_field_id(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    change: str,
    policy: str,
    order: str,
) -> None:
    """Each change, read plainly, through a row filter and through a mask on the changed column.

    Parameters
    ----------
    monkeypatch
        Replaces the file listing.
    session
        Database session.
    principal
        The reader, also the policies' principal.
    catalog
        The catalog.
    tmp_path
        The storage root.
    change
        The schema change between the two files.
    policy
        What applies to the reader.
    order
        Whether the data files are read in the order the manifests list them or the reverse; by-name
        reads used to take a column's type and presence from the first file.
    """
    column, values = _CHANGES[change](catalog, principal)
    if order == "reversed":
        real = pipeline_module.list_data_files

        def reverse(*args: object, **kwargs: object) -> list[str]:
            return list(reversed(real(*args, **kwargs)))  # ty: ignore[invalid-argument-type]

        monkeypatch.setattr(pipeline_module, "list_data_files", reverse)
    if policy == "row_filter":
        RowFilterService(session, AllowAllAuthorization(), catalog).create(
            principal,
            principal="admin",
            environment="dev",
            namespace="gold",
            table=change,
            expression="region = 'EU'",
        )
    if policy == "column_mask":
        ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
            principal,
            principal="admin",
            environment="dev",
            namespace="gold",
            table=change,
            column=column,
            expression="NULL",
        )

    read = _result(tmp_path, _query(session, principal, f"SELECT * FROM gold.{change}"))

    rows = sorted(read.to_pylist(), key=lambda row: row["id"])
    ids = [1, 3] if policy == "row_filter" else [1, 2, 3, 4]
    assert [row["id"] for row in rows] == ids
    expected = [None] * len(ids) if policy == "column_mask" else [values[i - 1] for i in ids]
    assert [row[column] for row in rows] == expected
    if policy != "column_mask" and change in _WIDENED:
        assert read.schema.field(column).type == _WIDENED[change]


_ADDRESS = StructType(
    NestedField(10, "country", StringType(), required=False),
    NestedField(11, "city", StringType(), required=False),
)


def test_a_renamed_field_inside_a_struct_column_fails_closed_until_the_rows_are_written_again(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """DuckDB matches a struct's fields by name: the old file would read `town` as NULL."""
    ref = _ref("customers")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(_optional(1, "id", LongType()), _optional(2, "address", _ADDRESS)),
    )
    city = pa.struct([pa.field("country", pa.string()), pa.field("city", pa.string())])
    town = pa.struct([pa.field("country", pa.string()), pa.field("town", pa.string())])
    old = pa.table(
        {
            "id": pa.array([1], pa.int64()),
            "address": pa.array([{"country": "DE", "city": "Berlin"}], city),
        }
    )
    table = _append(catalog, principal, ref, created, old)
    table = _evolve(
        catalog, principal, ref, table, lambda u: u.rename_column("address.city", "town")
    )
    new = pa.table(
        {
            "id": pa.array([2], pa.int64()),
            "address": pa.array([{"country": "FR", "town": "Paris"}], town),
        }
    )
    table = _append(catalog, principal, ref, table, new)

    refused = _query(session, principal, "SELECT * FROM gold.customers")

    assert refused.status == QueryRunStatus.FAILED
    assert refused.error_message == (
        "'gold.customers' cannot be read: the field `town` of its column `address` was renamed, "
        "moved, or dropped and added again after some of its data files were written, and Lakana "
        "reads the fields inside a nested column by name. Ask the owner of the table to write all "
        "of its rows again (an overwrite), so that every data file has the new layout."
    )
    assert str(tmp_path) not in refused.error_message
    assert ".parquet" not in refused.error_message

    rows = pa.table(
        {
            "id": pa.array([1, 2], pa.int64()),
            "address": pa.array(
                [{"country": "DE", "town": "Berlin"}, {"country": "FR", "town": "Paris"}], town
            ),
        }
    )
    transaction = StaticTable.from_metadata(table.metadata_location).transaction()
    transaction.overwrite(rows)
    catalog.commit_table(
        principal, ref, requirements=transaction._requirements, updates=transaction._updates
    )

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.customers"))
    assert sorted(row["address"]["town"] for row in read.to_pylist()) == ["Berlin", "Paris"]


def test_a_struct_whose_fields_never_changed_reads_whole_after_its_column_was_renamed(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The column's own rename is a field-id matter; its fields keep their names."""
    ref = _ref("places")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(_optional(1, "id", LongType()), _optional(2, "address", _ADDRESS)),
    )
    city = pa.struct([pa.field("country", pa.string()), pa.field("city", pa.string())])
    old = pa.table(
        {
            "id": pa.array([1], pa.int64()),
            "address": pa.array([{"country": "DE", "city": "Berlin"}], city),
        }
    )
    table = _append(catalog, principal, ref, created, old)
    _evolve(catalog, principal, ref, table, lambda u: u.rename_column("address", "home"))

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.places"))

    assert read.to_pylist() == [{"id": 1, "home": {"country": "DE", "city": "Berlin"}}]


@pytest.mark.parametrize(
    "field_type",
    [
        DecimalType(38, 9),
        DecimalType(10, 0),
        TimestamptzType(),
        StructType(
            NestedField(10, "a b", IntegerType(), required=False),
            NestedField(11, 'q"x', StringType(), required=False),
        ),
        ListType(20, MapType(30, StringType(), 31, DoubleType())),
        ListType(
            40, StructType(NestedField(41, "inner list", ListType(42, LongType()), required=False))
        ),
    ],
)
def test_every_duckdb_type_the_map_names_round_trips_through_duckdb(
    field_type: IcebergType,
) -> None:
    """DuckDB reads the rendered type back as the same type, with nothing dropped.

    DuckDB's `::` cast ignores text after a complete type, so a mis-rendered type would be cut
    short without an error; comparing `typeof` with the rendered text shows any such cut.

    Parameters
    ----------
    field_type
        An Iceberg type.
    """
    duckdb_type = duckdb_type_of(field_type)
    assert duckdb_type is not None
    row = duckdb.connect().execute(f"SELECT typeof(NULL::{duckdb_type})").fetchone()
    assert row is not None
    assert row[0].replace(" ", "") == duckdb_type.replace(" ", "")
