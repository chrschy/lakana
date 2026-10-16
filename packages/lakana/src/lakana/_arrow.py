"""Pyarrow schema/data conversions used by `lakana.resources.tables.Tables`.

The platform API's table-creation route accepts a caller-declared table
schema as a JSON column list (see
`lakana_server.api.platform.schemas.ColumnIn`), not an Arrow schema. This
module is the SDK-side half of that mapping, used by `Tables.create` to
convert a `pyarrow.Schema` into the JSON shape the route expects. It also
holds `cast_timestamp_ns_columns_to_us`, `Tables.append`'s pre-serialization
adjustment for the one column type shape the platform API cannot accept as
sent (a pandas DataFrame's default nanosecond timestamp precision), and
`json_to_arrow_type`, the inverse mapping used by the CLI (`lakana.cli._files`)
to cast a data file to a table's declared column types before appending it.
"""

from __future__ import annotations

import re

import pyarrow as pa

from lakana.errors import ValidationError

_SIMPLE_ARROW_TO_JSON: dict[pa.DataType, str] = {
    pa.int32(): "int",
    pa.int64(): "long",
    pa.float32(): "float",
    pa.float64(): "double",
    pa.bool_(): "boolean",
    pa.string(): "string",
    pa.large_string(): "string",
    pa.string_view(): "string",
    pa.binary(): "binary",
    pa.large_binary(): "binary",
    pa.binary_view(): "binary",
    pa.date32(): "date",
    pa.uuid(): "uuid",
}
"""Every supported Arrow type whose JSON name doesn't depend on a parameter.

`decimal128(p, s)`, `time64[us]`, and `timestamp[us]` (with or without a
timezone) are parametric and handled separately, in `_arrow_type_to_json`,
before this table is ever consulted.

`pa.uuid()` (`extension<arrow.uuid>`, storage `fixed_size_binary(16)`) maps
to `"uuid"` — the server's create-table route, its append allowlist, and
`lakana_core.catalog.json_schema.to_iceberg_schema` already accept a `uuid`
column; this entry was missing from the initial mapping table, which made the SDK refuse a type the
server itself supports.
"""

_UTC_ALIASES = frozenset({"UTC", "+00:00", "Etc/UTC", "Z"})
"""Mirrors pyiceberg's own `UTC_ALIASES` (`pyiceberg.io.pyarrow.UTC_ALIASES`,
also duplicated server-side in `lakana_core.catalog.append._types_compatible`'s
docstring) — the SDK does not import pyiceberg (`sdk-is-independent`), so
this is a hand-maintained copy, not an import. Only these four spellings of
UTC are treated as Iceberg's `timestamptz`; any other timezone is refused
outright rather than silently mapped to UTC, since
`timestamptz` always means UTC in Iceberg and a caller's non-UTC-aware
column would otherwise be reinterpreted at the wrong instant."""


def _arrow_type_to_json(arrow_type: pa.DataType, column_name: str) -> str:
    """Map one Arrow field type to its JSON column-schema type name.

    Parameters
    ----------
    arrow_type
        The Arrow type to convert.
    column_name
        The field's name, used only to name the column in the error message
        raised if `arrow_type` is not supported.

    Returns
    -------
    str
        The JSON type name the platform API's `ColumnIn.type` expects, e.g.
        `"long"` or `"decimal(10,2)"`.

    Raises
    ------
    ValidationError
        If `arrow_type` has no corresponding JSON column type, or is a
        timestamp with a timezone that is not one of `_UTC_ALIASES` — Iceberg's `timestamptz`
        always means UTC, so a
        non-UTC-aware column cannot be represented as one without silently
        reinterpreting its values at the wrong instant.
    """
    if pa.types.is_decimal128(arrow_type):
        return f"decimal({arrow_type.precision},{arrow_type.scale})"
    if pa.types.is_time64(arrow_type) and arrow_type.unit == "us":
        return "time"
    if pa.types.is_timestamp(arrow_type) and arrow_type.unit == "us":
        if arrow_type.tz is None:
            return "timestamp"
        if arrow_type.tz in _UTC_ALIASES:
            return "timestamptz"
        raise ValidationError(
            f"Column {column_name!r} has timezone {arrow_type.tz!r}; Iceberg's "
            "timestamptz is always UTC. Convert the column to a UTC-aware timestamp "
            "before creating the table."
        )
    mapped = _SIMPLE_ARROW_TO_JSON.get(arrow_type)
    if mapped is None:
        raise ValidationError(
            f"Column {column_name!r} has an unsupported Arrow type: {arrow_type}."
        )
    return mapped


def schema_to_columns(schema: pa.Schema) -> list[dict[str, object]]:
    """Convert a pyarrow schema to the platform API's JSON column list.

    Parameters
    ----------
    schema
        The schema to convert.

    Returns
    -------
    list of dict
        One `{"name", "type", "nullable"}` entry per field, in schema order.

    Raises
    ------
    ValidationError
        If any field's Arrow type has no corresponding JSON column type.
    """
    return [
        {
            "name": field.name,
            "type": _arrow_type_to_json(field.type, field.name),
            "nullable": field.nullable,
        }
        for field in schema
    ]


def cast_timestamp_ns_columns_to_us(table: pa.Table) -> pa.Table:
    """Cast every nanosecond-precision timestamp column to microsecond precision.

    The platform API only ever accepts `timestamp[us]` (see
    `_arrow_type_to_json`); pandas DataFrames converted to Arrow default to
    `timestamp[ns]`, which `tables.append` would otherwise refuse outright
    with an unhelpful "unsupported Arrow type" error. The cast is `safe=True`
    : it raises rather than silently truncating a
    column that actually holds sub-microsecond precision, since silently
    losing precision on someone's data is worse than refusing the append.

    Parameters
    ----------
    table
        The table to adjust. Not mutated; a new table is returned.

    Returns
    -------
    pa.Table
        `table`, with every `timestamp[ns]` column cast to the matching
        `timestamp[us]` type (preserving its timezone, if any). Columns of
        any other type are returned unchanged.

    Raises
    ------
    ValidationError
        If casting a column from nanosecond to microsecond precision would
        truncate a non-zero sub-microsecond value, naming that column.
    """
    for index, field in enumerate(table.schema):
        if not (pa.types.is_timestamp(field.type) and field.type.unit == "ns"):
            continue
        target_type = pa.timestamp("us", tz=field.type.tz)
        try:
            cast_column = table.column(index).cast(target_type, safe=True)
        except pa.ArrowInvalid as exc:
            raise ValidationError(
                f"Column {field.name!r} has nanosecond-precision timestamp values that "
                "would be truncated by the platform API's microsecond precision; round "
                "the column before appending."
            ) from exc
        table = table.set_column(
            index, pa.field(field.name, target_type, nullable=field.nullable), cast_column
        )
    return table


_JSON_TO_ARROW: dict[str, pa.DataType] = {
    "boolean": pa.bool_(),
    "int": pa.int32(),
    "long": pa.int64(),
    "float": pa.float32(),
    "double": pa.float64(),
    "date": pa.date32(),
    "time": pa.time64("us"),
    "timestamp": pa.timestamp("us"),
    "timestamptz": pa.timestamp("us", tz="UTC"),
    "string": pa.string(),
    "uuid": pa.uuid(),
    "binary": pa.binary(),
}
"""The Arrow type each JSON column type is written as — the inverse of
`_arrow_type_to_json` for the canonical Arrow spelling of each type."""

_DECIMAL = re.compile(r"decimal\(([0-9]+),([0-9]+)\)")


def json_to_arrow_type(type_name: str) -> pa.DataType:
    """Map a JSON column type (`"long"`, `"decimal(10,2)"`, ...) to its Arrow type.

    Parameters
    ----------
    type_name
        The type as the platform API spells it.

    Returns
    -------
    pa.DataType
        The Arrow type appends are sent as.

    Raises
    ------
    ValidationError
        If the type has no Arrow mapping here (e.g. a nested type).
    """
    match = _DECIMAL.fullmatch(type_name)
    if match:
        return pa.decimal128(int(match.group(1)), int(match.group(2)))
    try:
        return _JSON_TO_ARROW[type_name]
    except KeyError:
        raise ValidationError(f"Unsupported column type {type_name!r}.") from None


_VIEW_TO_PLAIN: dict[pa.DataType, pa.DataType] = {
    pa.string_view(): pa.string(),
    pa.binary_view(): pa.binary(),
}
"""View types (polars' default for text and bytes) and the plain type each is sent as."""


def normalize_view_columns(table: pa.Table) -> pa.Table:
    """Cast `string_view`/`binary_view` columns to `string`/`binary`.

    polars — Lakana's default DataFrame tool — exports text and bytes as Arrow
    view types. The append endpoint refuses view types by design (their
    validation cost isn't bounded by the payload size), so the SDK converts
    them client-side; the cast is lossless.

    Parameters
    ----------
    table
        The table to adjust. Not mutated; a new table is returned.

    Returns
    -------
    pa.Table
        `table`, with every view-typed column cast to its plain counterpart.
    """
    for index, field in enumerate(table.schema):
        plain = _VIEW_TO_PLAIN.get(field.type)
        if plain is not None:
            table = table.set_column(
                index,
                pa.field(field.name, plain, nullable=field.nullable),
                table.column(index).cast(plain),
            )
    return table
