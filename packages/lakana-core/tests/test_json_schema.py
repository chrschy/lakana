"""Tests for JSON-schema to Iceberg-schema conversion."""

import pytest
from pyiceberg.schema import Schema
from pyiceberg.types import (
    DecimalType,
    ListType,
    LongType,
    NestedField,
    StringType,
    TimestamptzType,
)

from lakana_core.catalog.json_schema import (
    _PRIMITIVE_TYPES,
    ColumnSpec,
    from_iceberg_schema,
    to_iceberg_schema,
)
from lakana_core.errors import ValidationError


def test_converts_primitives_with_ids_and_nullability() -> None:
    """Field ids are assigned in order; nullable=False becomes required."""
    schema = to_iceberg_schema(
        [
            ColumnSpec("id", "long", False),
            ColumnSpec("name", "string", True),
            ColumnSpec("at", "timestamptz", True),
        ]
    )
    fields = schema.fields
    assert [(f.field_id, f.name, f.required) for f in fields] == [
        (1, "id", True),
        (2, "name", False),
        (3, "at", False),
    ]
    assert isinstance(fields[0].field_type, LongType)
    assert isinstance(fields[1].field_type, StringType)
    assert isinstance(fields[2].field_type, TimestamptzType)


def test_decimal_parses_precision_and_scale() -> None:
    """decimal(p,s) becomes DecimalType(p, s)."""
    (field,) = to_iceberg_schema([ColumnSpec("price", "decimal(10,2)", True)]).fields
    assert field.field_type == DecimalType(10, 2)


_EXTENDED_ARABIC_INDIC_TEN = chr(0x6F1) + chr(0x6F0)
"""Extended Arabic-Indic digits for "10", built with `chr()` rather than
written as literal characters so ruff's ambiguous-unicode-character lint
(which flags a literal look-alike character in source text) has nothing to
flag -- `int()` parses this the same as the ASCII digits `"10"`, which is
exactly the gap `test_invalid_specs_are_refused` covers below."""


@pytest.mark.parametrize(
    "columns",
    [
        [],
        [ColumnSpec("a", "varchar", True)],
        [ColumnSpec("a", "long", True), ColumnSpec("a", "long", True)],
        [ColumnSpec("", "long", True)],
        [ColumnSpec("p", "decimal(39,2)", True)],
        [ColumnSpec("p", "decimal(5,6)", True)],
        [ColumnSpec("p", "decimal(x,2)", True)],
        [ColumnSpec("p", "decimal(10,2)\n", True)],
        [ColumnSpec("p", f"decimal({_EXTENDED_ARABIC_INDIC_TEN},2)", True)],
    ],
)
def test_invalid_specs_are_refused(columns: list[ColumnSpec]) -> None:
    r"""Unknown types, duplicates, empty names, bad decimals and empty lists are refused.

    The last two cases check that `_DECIMAL_PATTERN` uses
    `\Z` rather than `$` (which matches just before a trailing newline as
    well as at the true end of the string), so a trailing newline no longer
    slips through, and `[0-9]` rather than `\d` (which matches any Unicode
    decimal digit, not only ASCII digits), so `_EXTENDED_ARABIC_INDIC_TEN`
    -- a value `int()` still happily parses -- no longer slips past what
    looks like a strict `decimal(p,s)` check either.
    """
    with pytest.raises(ValidationError):
        to_iceberg_schema(columns)


def test_from_iceberg_schema_round_trips_every_supported_type() -> None:
    """`from_iceberg_schema` inverts `to_iceberg_schema` for every JSON type."""
    columns = [
        ColumnSpec(name=f"c_{i}", type=type_name, nullable=i % 2 == 0)
        for i, type_name in enumerate([*sorted(_PRIMITIVE_TYPES), "decimal(10,2)"])
    ]

    assert from_iceberg_schema(to_iceberg_schema(columns)) == columns


def test_from_iceberg_schema_names_a_nested_type_by_its_iceberg_string() -> None:
    """A type outside the JSON form (e.g. a list) is reported, not dropped."""
    schema = Schema(
        NestedField(1, "tags", ListType(2, StringType(), element_required=False), required=False)
    )

    [column] = from_iceberg_schema(schema)

    assert column.name == "tags"
    assert column.type.startswith("list<")
    assert column.nullable is True
