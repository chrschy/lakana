"""Tests for the field-id map of an Iceberg read: the DuckDB type of every Iceberg type."""

from __future__ import annotations

import pytest
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadata, TableMetadataV2
from pyiceberg.table.snapshots import Operation, Snapshot, Summary
from pyiceberg.table.sorting import SortOrder
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    FixedType,
    FloatType,
    IcebergType,
    IntegerType,
    ListType,
    LongType,
    MapType,
    NestedField,
    StringType,
    StructType,
    TimestampNanoType,
    TimestampType,
    TimestamptzNanoType,
    TimestamptzType,
    TimeType,
    UnknownType,
    UUIDType,
)

from lakana_core.catalog.field_ids import _schemas_of_live_files, duckdb_type_of, read_fields
from lakana_core.errors import UnsupportedTableError
from lakana_core.query_rewrite import ReadField

_TYPES = [
    (BooleanType(), "BOOLEAN"),
    (IntegerType(), "INTEGER"),
    (LongType(), "BIGINT"),
    (FloatType(), "FLOAT"),
    (DoubleType(), "DOUBLE"),
    (DecimalType(10, 2), "DECIMAL(10, 2)"),
    (DateType(), "DATE"),
    (TimeType(), "TIME"),
    (TimestampType(), "TIMESTAMP"),
    (TimestamptzType(), "TIMESTAMP WITH TIME ZONE"),
    (StringType(), "VARCHAR"),
    (UUIDType(), "UUID"),
    (BinaryType(), "BLOB"),
    (FixedType(16), "BLOB"),
    (
        StructType(
            NestedField(10, "a b", IntegerType(), required=False),
            NestedField(11, 'q"x', StringType(), required=False),
        ),
        'STRUCT("a b" INTEGER, "q""x" VARCHAR)',
    ),
    (ListType(20, LongType()), "BIGINT[]"),
    (MapType(30, StringType(), 31, DoubleType()), "MAP(VARCHAR, DOUBLE)"),
]
"""Every Iceberg type a read can meet, with the DuckDB type the map names for it."""


@pytest.mark.parametrize(("field_type", "expected"), _TYPES)
def test_every_iceberg_type_has_the_duckdb_type_the_map_names(
    field_type: IcebergType, expected: str
) -> None:
    """A field name inside a struct is quoted, with its own quotes doubled.

    Parameters
    ----------
    field_type
        The Iceberg type.
    expected
        Its DuckDB type.
    """
    assert duckdb_type_of(field_type) == expected


def test_read_fields_keeps_ids_names_and_order() -> None:
    """The map is the read's columns in the schema's order."""
    fields = [
        NestedField(7, "id", LongType(), required=True),
        NestedField(3, "name", StringType(), required=False),
    ]
    assert read_fields("gold.t", fields) == (
        ReadField(7, "id", "BIGINT"),
        ReadField(3, "name", "VARCHAR"),
    )


@pytest.mark.parametrize("field_type", [TimestampNanoType(), TimestamptzNanoType(), UnknownType()])
def test_a_type_duckdb_cannot_read_here_refuses_the_table(field_type: IcebergType) -> None:
    """Fail closed with the column and the type, rather than leave the column out.

    Parameters
    ----------
    field_type
        An Iceberg type the map has no DuckDB type for.
    """
    with pytest.raises(UnsupportedTableError) as caught:
        read_fields("gold.t", [NestedField(2, "x", field_type, required=False)])

    assert str(caught.value) == (
        f"'gold.t' has the column 'x' of type {field_type}, which Lakana cannot read yet. "
        "Ask the owner of the table to store the column as another type."
    )


def test_columns_that_differ_only_in_case_are_refused_with_a_next_step() -> None:
    """DuckDB cannot read two such columns under a map; the reader is told which and what to do."""
    fields = [
        NestedField(1, "Region", StringType(), required=False),
        NestedField(2, "region", StringType(), required=False),
    ]

    with pytest.raises(UnsupportedTableError) as caught:
        read_fields("gold.t", fields)

    assert str(caught.value) == (
        "'gold.t' has the columns 'Region' and 'region', which differ only in letter case, and "
        "Lakana compares column names without regard to case. Ask the owner of the table to "
        "rename one of them."
    )


def _metadata_with(*snapshots: Snapshot) -> TableMetadata:
    """Build the metadata of a table with two schemas and these snapshots.

    Parameters
    ----------
    *snapshots
        The snapshots, oldest first.

    Returns
    -------
    TableMetadata
        The metadata; its schemas have the ids 0 and 1.
    """
    return TableMetadataV2(
        location="file:///table",
        last_column_id=2,
        last_sequence_number=len(snapshots),
        schemas=[
            Schema(NestedField(1, "id", LongType(), required=False), schema_id=0),
            Schema(
                NestedField(1, "id", LongType(), required=False),
                NestedField(2, "x", StringType(), required=False),
                schema_id=1,
            ),
        ],
        current_schema_id=1,
        partition_specs=[PartitionSpec()],
        sort_orders=[SortOrder()],
        snapshots=list(snapshots),
    )


def _snapshot(
    snapshot_id: int, parent: int | None, schema_id: int | None, **summary: str
) -> Snapshot:
    """Build a snapshot.

    Parameters
    ----------
    snapshot_id
        Its id.
    parent
        Its parent's id.
    schema_id
        The schema it was written with.
    **summary
        Entries of its summary; none means it has no summary.

    Returns
    -------
    Snapshot
        The snapshot.
    """
    return Snapshot(
        snapshot_id=snapshot_id,
        parent_snapshot_id=parent,
        sequence_number=1,
        timestamp_ms=1,
        manifest_list=f"file:///table/metadata/snap-{snapshot_id}.avro",
        summary=Summary(Operation.APPEND, **summary) if summary else None,
        schema_id=schema_id,
    )


def test_an_unknown_snapshot_could_have_any_schema_of_the_table() -> None:
    """Without the snapshot, every schema is a candidate."""
    metadata = _metadata_with(_snapshot(1, None, 0))
    assert [schema.schema_id for schema in _schemas_of_live_files(metadata, 99)] == [0, 1]


def test_a_snapshot_with_a_schema_the_metadata_lacks_could_have_any_schema() -> None:
    """A schema id that names nothing is no evidence."""
    metadata = _metadata_with(_snapshot(1, None, 7))
    assert [schema.schema_id for schema in _schemas_of_live_files(metadata, 1)] == [0, 1]


def test_a_snapshot_without_a_summary_and_without_a_parent_ends_the_walk() -> None:
    """Nothing says that its files are all its own, but there is nothing further back."""
    metadata = _metadata_with(_snapshot(1, None, 0))
    assert [schema.schema_id for schema in _schemas_of_live_files(metadata, 1)] == [0]


def test_the_walk_goes_back_to_the_snapshot_that_added_every_live_file() -> None:
    """A child without a summary is followed to its parent, which added all its files."""
    metadata = _metadata_with(
        _snapshot(1, None, 0, **{"added-data-files": "2", "total-data-files": "2"}),
        _snapshot(2, 1, 1),
    )
    assert [schema.schema_id for schema in _schemas_of_live_files(metadata, 2)] == [1, 0]


def test_a_struct_with_a_nested_type_duckdb_has_none_for_has_no_type() -> None:
    """One unreadable field makes the whole struct unreadable."""
    nested = StructType(NestedField(2, "t", TimestampNanoType(), required=False))
    assert duckdb_type_of(nested) is None


def test_a_snapshot_that_added_only_some_of_its_files_is_followed_to_its_parent() -> None:
    """Its summary says that older files are live too, so the parent's schema counts."""
    metadata = _metadata_with(
        _snapshot(1, None, 0, **{"added-data-files": "2", "total-data-files": "2"}),
        _snapshot(2, 1, 1, **{"added-data-files": "1", "total-data-files": "3"}),
    )
    assert [schema.schema_id for schema in _schemas_of_live_files(metadata, 2)] == [1, 0]
