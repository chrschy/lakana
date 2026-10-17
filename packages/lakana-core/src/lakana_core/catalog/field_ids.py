"""The field-id map of an Iceberg read.

Lakana reads an Iceberg table's data files by field id, as the Iceberg specification requires:
DuckDB's `read_parquet(..., schema = MAP {field_id: {name, type, default_value}})` projects every
file onto the schema the read uses, so a renamed or re-added column reads its own data, a
widened one reads in its new type and a column a file lacks reads as NULL. The map covers
top-level columns. DuckDB matches the fields inside a struct, list or map by name, so a read
whose nested fields were renamed, moved or replaced since some of its files were written is
refused instead (`refuse_changed_nested_fields`).
"""

from __future__ import annotations

from collections.abc import Sequence

from pyiceberg.schema import Schema, index_name_by_id
from pyiceberg.table.metadata import TableMetadata
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

from lakana_core.errors import UnsupportedTableError
from lakana_core.query_rewrite import ReadField

_PRIMITIVES: dict[type[IcebergType], str] = {
    BooleanType: "BOOLEAN",
    IntegerType: "INTEGER",
    LongType: "BIGINT",
    FloatType: "FLOAT",
    DoubleType: "DOUBLE",
    DateType: "DATE",
    TimeType: "TIME",
    TimestampType: "TIMESTAMP",
    TimestamptzType: "TIMESTAMP WITH TIME ZONE",
    StringType: "VARCHAR",
    UUIDType: "UUID",
    BinaryType: "BLOB",
}
"""The DuckDB type of each Iceberg primitive. The nanosecond timestamps, `unknown` and the
geospatial types of format version 3 have none here: a table with one is refused."""


def _quoted(name: str) -> str:
    """Quote a field name for a DuckDB type text.

    Parameters
    ----------
    name
        The field's name, which another Iceberg client may have chosen freely.

    Returns
    -------
    str
        The name in double quotes, its own double quotes doubled.
    """
    return '"' + name.replace('"', '""') + '"'


def duckdb_type_of(field_type: IcebergType) -> str | None:
    """Spell an Iceberg type as DuckDB's type parser reads it.

    Parameters
    ----------
    field_type
        The type of a column or of a field inside one.

    Returns
    -------
    str or None
        The DuckDB type (`BIGINT`, `DECIMAL(10, 2)`, `STRUCT("a" INTEGER)`, `BIGINT[]`,
        `MAP(VARCHAR, DOUBLE)`), or `None` when it is, or holds, a type DuckDB has none for.
    """
    if isinstance(field_type, DecimalType):
        return f"DECIMAL({field_type.precision}, {field_type.scale})"
    if isinstance(field_type, FixedType):
        return "BLOB"
    if isinstance(field_type, StructType):
        parts = []
        for nested in field_type.fields:
            inner = duckdb_type_of(nested.field_type)
            if inner is None:
                return None
            parts.append(f"{_quoted(nested.name)} {inner}")
        return f"STRUCT({', '.join(parts)})"
    if isinstance(field_type, ListType):
        element = duckdb_type_of(field_type.element_type)
        return None if element is None else f"{element}[]"
    if isinstance(field_type, MapType):
        key = duckdb_type_of(field_type.key_type)
        value = duckdb_type_of(field_type.value_type)
        return None if key is None or value is None else f"MAP({key}, {value})"
    return _PRIMITIVES.get(type(field_type))


def read_fields(dotted: str, fields: Sequence[NestedField]) -> tuple[ReadField, ...]:
    """Build the field-id map of a read from the columns it reads.

    Parameters
    ----------
    dotted
        The table's dotted name, as the query names it.
    fields
        The columns the read returns, in order, from the schema the read uses.

    Returns
    -------
    tuple of ReadField
        One entry per column.

    Raises
    ------
    UnsupportedTableError
        If a column has a type DuckDB has none for. Leaving the column out would hide it
        without a word, so the read is refused, naming the column and its type.
    """
    entries = []
    for column in fields:
        duckdb_type = duckdb_type_of(column.field_type)
        if duckdb_type is None:
            raise UnsupportedTableError(
                f"{dotted!r} has the column {column.name!r} of type {column.field_type}, which "
                "Lakana cannot read yet. Ask the owner of the table to store the column as "
                "another type."
            )
        entries.append(ReadField(column.field_id, column.name, duckdb_type))
    return tuple(entries)


def _nested_layout(fields: Sequence[NestedField]) -> dict[int, tuple[int, str]]:
    """Place every field inside a nested column by its column's id and its path in the column.

    Parameters
    ----------
    fields
        Top-level columns.

    Returns
    -------
    dict
        Field id of each nested field to the id of its top-level column and its dotted path
        inside that column (`city`, `element`, `key`). A top-level rename leaves it unchanged.
    """
    layout: dict[int, tuple[int, str]] = {}
    for column in fields:
        for field_id, name in index_name_by_id(Schema(column)).items():
            if field_id != column.field_id:
                layout[field_id] = (column.field_id, name.split(".", 1)[1])
    return layout


def _schemas_of_live_files(metadata: TableMetadata, snapshot_id: int) -> list[Schema]:
    """List the schemas the data files of a snapshot may have been written with.

    The snapshot and its ancestors back to the first one that added all of its files
    (its summary's `added-data-files` equals `total-data-files`: an overwrite or the first
    write). Where an ancestor is missing or names no schema, every schema the table has had.

    Parameters
    ----------
    metadata
        The table's metadata.
    snapshot_id
        The snapshot the read reads.

    Returns
    -------
    list of Schema
        The schemas, without repeats.
    """
    by_id = {schema.schema_id: schema for schema in metadata.schemas}
    found: dict[int, None] = {}
    snapshot = metadata.snapshot_by_id(snapshot_id)
    while True:
        if snapshot is None or snapshot.schema_id is None or snapshot.schema_id not in by_id:
            return list(by_id.values())
        found[snapshot.schema_id] = None
        summary = snapshot.summary
        if summary is not None:
            added = summary.get("added-data-files")
            if added is not None and added == summary.get("total-data-files"):
                break
        if snapshot.parent_snapshot_id is None:
            break
        snapshot = metadata.snapshot_by_id(snapshot.parent_snapshot_id)
    return [by_id[schema_id] for schema_id in found]


def refuse_changed_nested_fields(
    dotted: str, metadata: TableMetadata, snapshot_id: int, fields: Sequence[NestedField]
) -> None:
    """Refuse a read whose nested fields changed since some of its data files were written.

    Parameters
    ----------
    dotted
        The table's dotted name, as the query names it.
    metadata
        The table's metadata.
    snapshot_id
        The snapshot the read reads.
    fields
        The columns the read returns, from the schema the read uses.

    Raises
    ------
    UnsupportedTableError
        If a field inside a struct, list or map column has another place in a schema the
        snapshot's files may have been written with, or its place another field. The message
        names the table, the column and the field, and no file.
    """
    now = _nested_layout(fields)
    if not now:
        return
    by_place = {place: field_id for field_id, place in now.items()}
    names = {column.field_id: column.name for column in fields}
    for schema in _schemas_of_live_files(metadata, snapshot_id):
        for field_id, place in _nested_layout(schema.fields).items():
            moved = field_id in now and now[field_id] != place
            replaced = place in by_place and by_place[place] != field_id
            if moved or replaced:
                column_id, path = now[field_id] if moved else place
                raise UnsupportedTableError(
                    f"{dotted!r} cannot be read: the field `{path}` of its column "
                    f"`{names[column_id]}` was renamed, moved, or dropped and added again after "
                    "some of its data files were written, and Lakana reads the fields inside a "
                    "nested column by name. Ask the owner of the table to write all of its rows "
                    "again (an overwrite), so that every data file has the new layout."
                )
