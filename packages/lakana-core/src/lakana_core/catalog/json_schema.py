"""Convert a caller-declared, JSON-ish column list into an Iceberg `Schema`.

This is the SDK's entry point for `create_table`-from-schema: a caller
that already has data, not an Iceberg `Schema` object, describes columns as
plain `(name, type, nullable)` triples, and this module is the only place
that knows how those type names map onto pyiceberg's type classes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pyiceberg.schema import Schema
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    FloatType,
    IcebergType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestampType,
    TimestamptzType,
    TimeType,
    UUIDType,
)

from lakana_core.errors import ValidationError

_PRIMITIVE_TYPES: dict[str, IcebergType] = {
    "boolean": BooleanType(),
    "int": IntegerType(),
    "long": LongType(),
    "float": FloatType(),
    "double": DoubleType(),
    "date": DateType(),
    "time": TimeType(),
    "timestamp": TimestampType(),
    "timestamptz": TimestamptzType(),
    "string": StringType(),
    "uuid": UUIDType(),
    "binary": BinaryType(),
}
"""Every supported type name that isn't parametric, mapped to its pyiceberg type."""

_DECIMAL_PATTERN = re.compile(r"^decimal\(([0-9]+),([0-9]+)\)\Z")
"""Matches `decimal(p,s)`, capturing precision and scale as digit strings.

`[0-9]`, not `\\d`: `\\d` matches any Unicode decimal digit character (e.g.
Arabic-Indic digits), not just ASCII `0`-`9`, which `int()` nonetheless
happily parses -- so `\\d` would have silently accepted a precision/scale
written in another digit script. `\\Z`, not `$`: `$` matches immediately
before a trailing `\\n` as well as at the true end of the string, so
`"decimal(10,2)\\n"` would otherwise pass this pattern."""


@dataclass(frozen=True)
class ColumnSpec:
    """One caller-declared column, before it becomes an Iceberg field.

    Parameters
    ----------
    name
        Column name.
    type
        An Iceberg primitive type name — one of `boolean`, `int`, `long`,
        `float`, `double`, `decimal(p,s)`, `date`, `time`, `timestamp`,
        `timestamptz`, `string`, `uuid`, or `binary`.
    nullable
        Whether the column may hold nulls. `False` becomes `required=True`
        on the resulting Iceberg field.
    """

    name: str
    type: str
    nullable: bool


def _parse_type(type_name: str) -> IcebergType:
    """Resolve a declared type name to an Iceberg primitive type.

    Parameters
    ----------
    type_name
        The declared type name, such as `"long"` or `"decimal(10,2)"`.

    Returns
    -------
    IcebergType
        The corresponding Iceberg type.

    Raises
    ------
    ValidationError
        If `type_name` is not one of the supported primitives and does not
        match `decimal(p,s)` with `1 <= p <= 38` and `0 <= s <= p`.
    """
    primitive = _PRIMITIVE_TYPES.get(type_name)
    if primitive is not None:
        return primitive
    match = _DECIMAL_PATTERN.match(type_name)
    if match is None:
        raise ValidationError(f"Unknown column type: {type_name!r}.")
    precision, scale = int(match.group(1)), int(match.group(2))
    if not (1 <= precision <= 38):
        raise ValidationError(
            f"decimal precision must be between 1 and 38; got {precision} in {type_name!r}."
        )
    if not (0 <= scale <= precision):
        raise ValidationError(
            f"decimal scale must be between 0 and its precision; got {scale} in {type_name!r}."
        )
    return DecimalType(precision, scale)


def to_iceberg_schema(columns: Sequence[ColumnSpec]) -> Schema:
    """Build an Iceberg `Schema` from a caller-declared column list.

    Parameters
    ----------
    columns
        The columns, in order. Field ids are assigned `1..n` in this order,
        matching the order pyiceberg itself uses to number a freshly created
        table's schema.

    Returns
    -------
    Schema
        The resulting schema.

    Raises
    ------
    ValidationError
        If `columns` is empty; if a column's name is empty or repeated; or
        if a column's `type` is not a recognised primitive or a valid
        `decimal(p,s)`.
    """
    if not columns:
        raise ValidationError("A table needs at least one column.")
    seen: set[str] = set()
    fields: list[NestedField] = []
    for field_id, column in enumerate(columns, start=1):
        if not column.name:
            raise ValidationError("Column names must not be empty.")
        if column.name in seen:
            raise ValidationError(f"Duplicate column name: {column.name!r}.")
        seen.add(column.name)
        fields.append(
            NestedField(
                field_id, column.name, _parse_type(column.type), required=not column.nullable
            )
        )
    return Schema(*fields)


def from_iceberg_schema(schema: Schema) -> list[ColumnSpec]:
    """Describe an Iceberg schema in the API's JSON column form.

    The inverse of `to_iceberg_schema` for every type that form supports. A
    type outside it (a nested `list`/`struct`/`map`) is reported by its
    Iceberg string rather than dropped, so a caller still sees every column.

    Parameters
    ----------
    schema
        The table's current Iceberg schema.

    Returns
    -------
    list of ColumnSpec
        One entry per top-level field, in schema order.
    """
    return [
        ColumnSpec(name=field.name, type=type_name(field.field_type), nullable=not field.required)
        for field in schema.fields
    ]


def type_name(iceberg_type: IcebergType) -> str:
    """Name an Iceberg type the way the JSON column form spells it.

    Parameters
    ----------
    iceberg_type
        The field's type.

    Returns
    -------
    str
        `"long"`, `"decimal(10,2)"`, ... — or `str(iceberg_type)` for a type
        the JSON form has no name for.
    """
    if isinstance(iceberg_type, DecimalType):
        return f"decimal({iceberg_type.precision},{iceberg_type.scale})"
    for name, primitive in _PRIMITIVE_TYPES.items():
        if iceberg_type == primitive:
            return name
    return str(iceberg_type)
