"""The DuckDB type a column mask is cast to, built as a structured sqlglot type.

The target of the cast is never parsed from a type string. An Iceberg column maps from its
`pyiceberg` type object; a raw-zone (external) column maps from the Arrow type name stored when
the table was created, through a closed list of the exact names pyarrow prints for the types
below. Each result is a fresh `sqlglot.expressions.DataType` made of a type constant and, for a
decimal, two integers, so nothing a file or a column name contains can reach the SQL.

A column of any other type (a struct, a list, a map, a nanosecond or second timestamp, an
unsigned integer, an unknown type) has no cast: the mask is used as it is. The creation check
(`lakana_core.policy.expression_types`) accepts only `NULL` or the column itself as the mask of
such a column, so there is nothing for a cast to keep.
"""

from __future__ import annotations

import sqlglot.expressions as exp
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
    LongType,
    StringType,
    TimestampType,
    TimestamptzType,
    TimeType,
    UUIDType,
)

_ICEBERG: dict[type, exp.DType] = {
    BooleanType: exp.DType.BOOLEAN,
    IntegerType: exp.DType.INT,
    LongType: exp.DType.BIGINT,
    FloatType: exp.DType.FLOAT,
    DoubleType: exp.DType.DOUBLE,
    DateType: exp.DType.DATE,
    TimeType: exp.DType.TIME,
    TimestampType: exp.DType.TIMESTAMP,
    TimestamptzType: exp.DType.TIMESTAMPTZ,
    StringType: exp.DType.VARCHAR,
    UUIDType: exp.DType.UUID,
    BinaryType: exp.DType.VARBINARY,
    FixedType: exp.DType.VARBINARY,
}
"""The type of each Iceberg primitive, as the field-id read (`duckdb_type_of`) gives it."""

_ARROW: dict[str, exp.DType] = {
    "int8": exp.DType.TINYINT,
    "int16": exp.DType.SMALLINT,
    "int32": exp.DType.INT,
    "int64": exp.DType.BIGINT,
    "float": exp.DType.FLOAT,
    "double": exp.DType.DOUBLE,
    "bool": exp.DType.BOOLEAN,
    "string": exp.DType.VARCHAR,
    "large_string": exp.DType.VARCHAR,
    "binary": exp.DType.VARBINARY,
    "large_binary": exp.DType.VARBINARY,
    "date32[day]": exp.DType.DATE,
    "timestamp[us]": exp.DType.TIMESTAMP,
}
"""The exact names pyarrow prints for the supported Arrow types."""

_MAX_PRECISION = 38
_TZ_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_+-:")


def _decimal(precision: int, scale: int) -> exp.DataType:
    """Build `DECIMAL(precision, scale)` from two integers.

    Parameters
    ----------
    precision
        The decimal's precision.
    scale
        The decimal's scale.

    Returns
    -------
    sqlglot.expressions.DataType
        The type, its parameters integer literals.
    """
    return exp.DataType(
        this=exp.DType.DECIMAL,
        expressions=[
            exp.DataTypeParam(this=exp.Literal.number(precision)),
            exp.DataTypeParam(this=exp.Literal.number(scale)),
        ],
    )


def iceberg_cast_type(field_type: IcebergType) -> exp.DataType | None:
    """Give the cast target of an Iceberg column.

    Parameters
    ----------
    field_type
        The column's Iceberg type.

    Returns
    -------
    sqlglot.expressions.DataType or None
        The type, or `None` for a struct, list, map or any type outside the supported primitives.
    """
    if isinstance(field_type, DecimalType):
        return _decimal(field_type.precision, field_type.scale)
    kind = _ICEBERG.get(type(field_type))
    return None if kind is None else exp.DataType(this=kind)


def arrow_cast_type(stored: str) -> exp.DataType | None:
    """Give the cast target of a raw-zone column from its stored Arrow type name.

    Parameters
    ----------
    stored
        The type as `schema_inference.infer_schema` stored it (pyarrow's printed name).

    Returns
    -------
    sqlglot.expressions.DataType or None
        The type, or `None` unless `stored` is exactly one of the supported names.
    """
    kind = _ARROW.get(stored)
    if kind is not None:
        return exp.DataType(this=kind)
    prefix = "timestamp[us, tz="
    if stored.startswith(prefix) and stored.endswith("]"):
        zone = stored[len(prefix) : -1]
        if zone and all(character in _TZ_CHARACTERS for character in zone):
            return exp.DataType(this=exp.DType.TIMESTAMPTZ)
        return None
    head = "decimal128("
    if stored.startswith(head) and stored.endswith(")"):
        first, comma, second = stored[len(head) : -1].partition(", ")
        if comma and all(part.isascii() and part.isdecimal() for part in (first, second)):
            precision, scale = int(first), int(second)
            valid = 1 <= precision <= _MAX_PRECISION and 0 <= scale <= precision
            if valid and stored == f"decimal128({precision}, {scale})":
                return _decimal(precision, scale)
    return None
