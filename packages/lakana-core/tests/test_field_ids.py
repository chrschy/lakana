"""Tests for the field-id map of an Iceberg read: the DuckDB type of every Iceberg type."""

from __future__ import annotations

import pytest
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

from lakana_core.catalog.field_ids import duckdb_type_of, read_fields
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
