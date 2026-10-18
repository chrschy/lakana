"""Tests for the structured CAST targets of column masks."""

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
    TimestampType,
    TimestamptzType,
    TimeType,
    UUIDType,
)

from lakana_core.policy.cast_types import arrow_cast_type, iceberg_cast_type


@pytest.mark.parametrize(
    ("iceberg", "rendered"),
    [
        (BooleanType(), "BOOLEAN"),
        (IntegerType(), "INT"),
        (LongType(), "BIGINT"),
        (FloatType(), "REAL"),
        (DoubleType(), "DOUBLE"),
        (DecimalType(10, 2), "DECIMAL(10, 2)"),
        (DateType(), "DATE"),
        (TimeType(), "TIME"),
        (TimestampType(), "TIMESTAMP"),
        (TimestamptzType(), "TIMESTAMPTZ"),
        (StringType(), "TEXT"),
        (UUIDType(), "UUID"),
        (BinaryType(), "BLOB"),
        (FixedType(16), "BLOB"),
    ],
)
def test_an_iceberg_primitive_maps_to_a_structured_duckdb_type(
    iceberg: IcebergType, rendered: str
) -> None:
    """Each supported primitive renders as the DuckDB type the field-id read gives it."""
    target = iceberg_cast_type(iceberg)
    assert target is not None
    assert target.sql(dialect="duckdb") == rendered


def test_a_nested_iceberg_type_has_no_cast() -> None:
    """A struct, a list and a map are not cast: a mask on one is NULL or the column itself."""
    struct = StructType(NestedField(1, "a DECIMAL(10, (SELECT 42)) , b", LongType(), False))
    assert iceberg_cast_type(struct) is None
    assert iceberg_cast_type(ListType(2, LongType(), element_required=False)) is None
    assert iceberg_cast_type(MapType(3, StringType(), 4, LongType(), value_required=False)) is None


@pytest.mark.parametrize(
    ("stored", "rendered"),
    [
        ("int8", "TINYINT"),
        ("int16", "SMALLINT"),
        ("int32", "INT"),
        ("int64", "BIGINT"),
        ("float", "REAL"),
        ("double", "DOUBLE"),
        ("bool", "BOOLEAN"),
        ("string", "TEXT"),
        ("large_string", "TEXT"),
        ("binary", "BLOB"),
        ("large_binary", "BLOB"),
        ("date32[day]", "DATE"),
        ("timestamp[us]", "TIMESTAMP"),
        ("timestamp[us, tz=UTC]", "TIMESTAMPTZ"),
        ("timestamp[us, tz=Europe/Berlin]", "TIMESTAMPTZ"),
        ("decimal128(10, 2)", "DECIMAL(10, 2)"),
        ("decimal128(38, 0)", "DECIMAL(38, 0)"),
    ],
)
def test_a_stored_arrow_type_name_maps_through_a_closed_list(stored: str, rendered: str) -> None:
    """Only the exact names pyarrow prints for the supported types give a cast."""
    target = arrow_cast_type(stored)
    assert target is not None
    assert target.sql(dialect="duckdb") == rendered


@pytest.mark.parametrize(
    "stored",
    [
        "struct<a: int64>",
        "struct<a DECIMAL(10, (SELECT 42)) , b: int64>",
        "list<item: int64>",
        "map<string, int64>",
        "timestamp[ns]",
        "timestamp[s]",
        "timestamp[ms]",
        "date64[ms]",
        "uint8",
        "null",
        "decimal128(39, 2)",
        "decimal128(10, 11)",
        "decimal128(10, 2) ",
        "decimal128(1_0, 2)",
        "decimal128(10,2)",
        "DECIMAL(10, (SELECT 42))",
        "timestamp[us, tz=UTC) , x",
        "timestamp[us, tz=",
        "timestamp[us, tz=]",
        "timestamp[us, tz=UTC) , x]",
        "timestamp[us, tz=a b]",
        "",
        "string; DROP TABLE x",
    ],
)
def test_any_other_stored_arrow_type_name_has_no_cast(stored: str) -> None:
    """A type outside the list, or free text, is never turned into a cast."""
    assert arrow_cast_type(stored) is None
