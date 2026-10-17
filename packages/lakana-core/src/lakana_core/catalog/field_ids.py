"""The field-id map of an Iceberg read.

Lakana reads an Iceberg table's data files by field id, as the Iceberg specification requires:
DuckDB's `read_parquet(..., schema = MAP {field_id: {name, type, default_value}})` projects every
file onto the schema the read uses, so a renamed or re-added column reads its own data, a
widened one reads in its new type and a column a file lacks reads as NULL. The map covers
top-level columns. DuckDB matches the fields inside a struct, list or map by name, so a read
whose nested fields were renamed, moved or replaced since some of its files were written is
refused instead (`refuse_changed_nested_fields`).

A commit through the catalog that adds a data file without field ids is refused
(`check_added_files_have_field_ids`): such a file would read as NULL in every column.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

import pyarrow.parquet as pq
from pyiceberg.io import FileIO
from pyiceberg.manifest import (
    DataFile,
    DataFileContent,
    FileFormat,
    ManifestContent,
    ManifestEntryStatus,
)
from pyiceberg.schema import Schema, index_name_by_id
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.snapshots import Snapshot
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

from lakana_core.errors import UnsupportedTableError, ValidationError
from lakana_core.query_rewrite import ReadField
from lakana_core.storage.resolver import is_within_location

logger = logging.getLogger(__name__)

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


_FIELD_ID = b"PARQUET:field_id"
"""The Arrow field metadata key under which Parquet carries an Iceberg field id."""

_FOOTER_READERS = 8
"""How many data-file footers one commit reads at the same time."""

_NO_FIELD_IDS = (
    "The commit adds `{file}` to `{table}`, which has no Iceberg field ids. Lakana reads Iceberg "
    "data by field id, as the Iceberg specification requires. Write the file with a client that "
    "sets field ids."
)
_UNREADABLE_FILE = (
    "The commit adds `{file}` to `{table}`, which could not be read as a Parquet file, so "
    "nothing was applied. Write the data file before the commit, as Parquet with Iceberg field "
    "ids."
)
_NOT_PARQUET = (
    "The commit adds `{file}` to `{table}` as {format}, so nothing was applied. Lakana reads "
    "Parquet data files only; write the file as Parquet."
)
_UNREADABLE_MANIFEST = (
    "A manifest of the commit's snapshot for `{table}` could not be read, so nothing was "
    "applied. Write the manifests and the manifest list before the commit."
)


def has_field_ids(file_io: FileIO, path: str) -> bool:
    """Tell whether every top-level column of a Parquet file carries an Iceberg field id.

    Parameters
    ----------
    file_io
        Reads the table's storage.
    path
        The data file's URI.

    Returns
    -------
    bool
        Whether each column of the footer's schema has ``PARQUET:field_id`` metadata. Only
        the footer is read.
    """
    with file_io.new_input(path).open() as stream:
        schema = pq.read_schema(stream)
    return all(field.metadata is not None and _FIELD_ID in field.metadata for field in schema)


def _footer(file_io: FileIO, path: str) -> bool | None:
    """Read one footer for the commit check, without letting its error text out.

    Parameters
    ----------
    file_io
        Reads the table's storage.
    path
        The data file's URI.

    Returns
    -------
    bool or None
        `has_field_ids`'s answer, or `None` when the footer could not be read; the log then
        has the exception's class, never its text, which can name storage details.
    """
    try:
        return has_field_ids(file_io, path)
    except Exception as exc:  # any reader failure refuses the commit with a fixed text
        logger.warning("A data file of a commit could not be read (%s).", type(exc).__name__)
        return None


def _added_data_files(
    snapshot: Snapshot, metadata: TableMetadata, file_io: FileIO, table: str
) -> list[DataFile]:
    """List the data files a snapshot adds, from the manifests its parent does not have.

    Parameters
    ----------
    snapshot
        The snapshot a commit adds; its manifest list lies under the table's location.
    metadata
        The table's metadata before the commit.
    file_io
        Reads the table's storage.
    table
        The table's dotted name, for the message.

    Returns
    -------
    list of DataFile
        The live entries of status `ADDED` and content data, in manifest order. A manifest
        outside the table's location is not opened: a read refuses such a table anyway.

    Raises
    ------
    ValidationError
        If the manifest list or a manifest cannot be read.
    """
    parent = (
        None
        if snapshot.parent_snapshot_id is None
        else metadata.snapshot_by_id(snapshot.parent_snapshot_id)
    )
    try:
        known = set() if parent is None else {m.manifest_path for m in parent.manifests(file_io)}
        added = []
        for manifest in snapshot.manifests(file_io):
            if (
                manifest.manifest_path in known
                or manifest.content != ManifestContent.DATA
                or not is_within_location(metadata.location, manifest.manifest_path)
            ):
                continue
            for entry in manifest.fetch_manifest_entry(file_io, discard_deleted=True):
                if (
                    entry.status == ManifestEntryStatus.ADDED
                    and entry.data_file.content == DataFileContent.DATA
                ):
                    added.append(entry.data_file)
    except Exception as exc:  # any reader failure refuses the commit with a fixed text
        raise ValidationError(_UNREADABLE_MANIFEST.format(table=table)) from exc
    return added


def check_added_files_have_field_ids(
    snapshot: Snapshot, metadata: TableMetadata, file_io: FileIO, table: str
) -> None:
    """Refuse a snapshot that adds a data file without Iceberg field ids.

    Only files the commit adds are read, each footer once, at most `_FOOTER_READERS` at a
    time, and only under the table's location: a file elsewhere, or one whose path holds a
    glob character, is not opened, and a read refuses the table for it. Files committed before
    this check existed are not looked at. An entry a client marks `EXISTING` in a new manifest
    is not looked at either; such a file reads as NULL, which hides nothing a policy protects.

    Parameters
    ----------
    snapshot
        The snapshot the commit adds.
    metadata
        The table's metadata before the commit.
    file_io
        Reads the table's storage.
    table
        The table's dotted name, for the message.

    Raises
    ------
    ValidationError
        For the first added file, in manifest order, that is not Parquet, cannot be read, or
        lacks field ids; or if a manifest cannot be read. The message names that file and the
        table, nothing else.
    """
    files = _added_data_files(snapshot, metadata, file_io, table)
    for data_file in files:
        if data_file.file_format != FileFormat.PARQUET:
            raise ValidationError(
                _NOT_PARQUET.format(
                    file=data_file.file_path, table=table, format=data_file.file_format.value
                )
            )
    paths = [f.file_path for f in files if is_within_location(metadata.location, f.file_path)]
    pool = ThreadPoolExecutor(max_workers=_FOOTER_READERS)
    try:
        for path, outcome in zip(
            paths, pool.map(lambda p: _footer(file_io, p), paths), strict=True
        ):
            if outcome is None:
                raise ValidationError(_UNREADABLE_FILE.format(file=path, table=table))
            if not outcome:
                raise ValidationError(_NO_FIELD_IDS.format(file=path, table=table))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
