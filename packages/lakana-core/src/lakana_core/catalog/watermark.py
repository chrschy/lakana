"""The incremental-append watermark.

The watermark is `max(<key>)` over a target's current snapshot, read from
the Iceberg manifests' per-file upper bounds rather than by scanning rows
: exact for every allowed key type, and it never reads the
target's data. It is stored on the run as a type-tagged string and bound to
the wrapped query as the DuckDB variable `v_watermark`, which the wrapped
query reads with `getvariable`; it is never rendered into SQL text, and no
user SQL can read or set a variable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from pyiceberg.conversions import from_bytes
from pyiceberg.io import FileIO
from pyiceberg.manifest import DataFileContent
from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.types import (
    DateType,
    DecimalType,
    IcebergType,
    IntegerType,
    LongType,
    NestedField,
    TimestampType,
    TimestamptzType,
)
from sqlglot import exp

from lakana_core.errors import UnsupportedTableError, ValidationError

WatermarkValue = int | Decimal | dt.date | dt.datetime
"""A watermark's Python value, as DuckDB binds it."""

INCREMENTAL_KEY_TYPES = ("int", "long", "decimal", "date", "timestamp", "timestamptz")
"""The key types an incremental append accepts."""

WATERMARK_VARIABLE = "v_watermark"
"""The variable the query process sets the watermark to before it runs the wrapped query."""

_EPOCH_DATE = dt.date(1970, 1, 1)
_EPOCH = dt.datetime(1970, 1, 1)


def _tag(field_type: IcebergType) -> str | None:
    """Name a key type's watermark tag.

    Parameters
    ----------
    field_type
        The key column's Iceberg type.

    Returns
    -------
    str or None
        One of `INCREMENTAL_KEY_TYPES`, or `None` for a type that can't key
        an incremental append.
    """
    if isinstance(field_type, IntegerType):
        return "int"
    if isinstance(field_type, LongType):
        return "long"
    if isinstance(field_type, DecimalType):
        return "decimal"
    if isinstance(field_type, DateType):
        return "date"
    if isinstance(field_type, TimestamptzType):
        return "timestamptz"
    if isinstance(field_type, TimestampType):
        return "timestamp"
    return None


@dataclass(frozen=True)
class Watermark:
    """The highest key value a target already holds.

    Parameters
    ----------
    tag
        One of `INCREMENTAL_KEY_TYPES`.
    value
        The value: an `int`, a `Decimal`, a `date`, a naive `datetime`
        (``timestamp``) or a UTC-aware one (``timestamptz``).
    """

    tag: str
    value: WatermarkValue

    def encode(self) -> str:
        """Render the type-tagged string stored on the run.

        Returns
        -------
        str
            ``"<tag>:<value>"``, dates and times in ISO 8601.
        """
        if isinstance(self.value, dt.date):
            return f"{self.tag}:{self.value.isoformat()}"
        return f"{self.tag}:{self.value}"

    @classmethod
    def decode(cls, text: str) -> Watermark:
        """Parse a stored watermark.

        Parameters
        ----------
        text
            An `encode` result.

        Returns
        -------
        Watermark
            The watermark.

        Raises
        ------
        ValidationError
            If `text` has no tag or an unknown one.
        """
        tag, separator, raw = text.partition(":")
        if not separator or tag not in INCREMENTAL_KEY_TYPES:
            raise ValidationError(f"Not a watermark: {text!r}.")
        if tag in ("int", "long"):
            return cls(tag, int(raw))
        if tag == "decimal":
            return cls(tag, Decimal(raw))
        if tag == "date":
            return cls(tag, dt.date.fromisoformat(raw))
        return cls(tag, dt.datetime.fromisoformat(raw))


def check_incremental_key(schema: Schema, key: str, *, table_name: str) -> NestedField:
    """Require `key` to be a top-level column of an orderable type.

    Parameters
    ----------
    schema
        The schema to check.
    key
        The incremental key.
    table_name
        The table's dotted name, for the message.

    Returns
    -------
    NestedField
        The key's field.

    Raises
    ------
    ValidationError
        If `key` is not a top-level column, or its type is not one of
        `INCREMENTAL_KEY_TYPES`.
    """
    field = {f.name: f for f in schema.fields}.get(key)
    if field is None:
        raise ValidationError(f"Incremental key {key!r} is not a column of {table_name}.")
    if _tag(field.field_type) is None:
        raise ValidationError(
            f"Incremental key {key!r} has type {field.field_type}; it must be an int, long, "
            "decimal, date, timestamp or timestamptz column."
        )
    return field


def _decode_bound(field: NestedField, raw: bytes) -> Watermark:
    """Decode one file's upper bound for the key.

    Parameters
    ----------
    field
        The key's field.
    raw
        The serialized bound.

    Returns
    -------
    Watermark
        The bound as a watermark.
    """
    tag = _tag(field.field_type)
    value = from_bytes(field.field_type, raw)
    if tag == "date":
        value = _EPOCH_DATE + dt.timedelta(days=value)
    elif tag == "timestamp":
        value = _EPOCH + dt.timedelta(microseconds=value)
    elif tag == "timestamptz":
        value = (_EPOCH + dt.timedelta(microseconds=value)).replace(tzinfo=dt.UTC)
    return Watermark(str(tag), value)


def compute_watermark(metadata: TableMetadata, file_io: FileIO, key: str) -> Watermark | None:
    """Compute `max(key)` over a table's current snapshot from its manifests.

    Parameters
    ----------
    metadata
        The target's metadata.
    file_io
        Used to read the manifests.
    key
        The incremental key.

    Returns
    -------
    Watermark or None
        The highest upper bound over every live data file, or `None` when
        the table has no snapshot, no rows, or the key is NULL in every row.

    Raises
    ------
    ValidationError
        If `key` fails `check_incremental_key`, or a data file holds
        non-null key values but records no upper bound for it (column
        metrics disabled), so the watermark can't be known.
    UnsupportedTableError
        If the snapshot has a live row-level delete file.
    """
    field = check_incremental_key(metadata.schema(), key, table_name="the target")
    snapshot = metadata.current_snapshot()
    if snapshot is None:
        return None
    best: Watermark | None = None
    for manifest in snapshot.manifests(file_io):
        for entry in manifest.fetch_manifest_entry(file_io, discard_deleted=True):
            data_file = entry.data_file
            if data_file.content != DataFileContent.DATA:
                raise UnsupportedTableError(
                    "The target has row-level delete files; its watermark can't be read."
                )
            upper = (data_file.upper_bounds or {}).get(field.field_id)
            if upper is None:
                values = (data_file.value_counts or {}).get(field.field_id)
                nulls = (data_file.null_value_counts or {}).get(field.field_id, 0)
                if values is not None and values == nulls:
                    continue
                raise ValidationError(
                    f"The target's data files record no statistics for {key!r}, so its "
                    "watermark can't be read; enable column metrics for it."
                )
            candidate = _decode_bound(field, upper)
            if best is None or candidate.value > best.value:  # ty: ignore[unsupported-operator]
                best = candidate
    return best


@dataclass(frozen=True)
class IncrementalQuery:
    """A query wrapped to return only rows past the watermark.

    Parameters
    ----------
    sql
        The wrapped SQL.
    parameter
        The variable the watermark must be set as, `WATERMARK_VARIABLE`.
    """

    sql: str
    parameter: str


def wrap_incremental(sql: str, key: str) -> IncrementalQuery:
    """Wrap a rewritten query so it returns only rows past the watermark.

    The watermark is read from a variable the query process sets, beside the rewriter's own
    file-list variables; it is never written into the SQL. `sql` is the rewriter's
    sqlglot-regenerated output, never raw user text.

    Parameters
    ----------
    sql
        The rewritten query.
    key
        The incremental key, quoted as an identifier.

    Returns
    -------
    IncrementalQuery
        ``SELECT * FROM (<sql>) AS __lakana_incremental WHERE "<key>" > getvariable('v_watermark')``
        and the variable to set.
    """
    column = exp.to_identifier(key, quoted=True).sql(dialect="duckdb")
    # B608: `sql` is regenerated rewriter output, `column` a quoted identifier and the
    # variable name a constant; the watermark is the variable's value, never text.
    wrapped = (
        f"SELECT * FROM ({sql}) AS __lakana_incremental "  # nosec B608
        f"WHERE {column} > getvariable('{WATERMARK_VARIABLE}')"
    )
    return IncrementalQuery(sql=wrapped, parameter=WATERMARK_VARIABLE)
