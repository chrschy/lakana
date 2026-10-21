"""Helpers behind the governed append write path.

`CatalogService.append` is the orchestration: it authorizes `WRITE_TABLE`,
loads the table, and publishes the new snapshot through
`CatalogService.commit_table` so a governed append is subject to the same
the snapshot guards (location immutability, manifest-list confinement) as
every other write. This module holds the pieces that don't need a
`CatalogService` at all — schema validation, staging the pyiceberg
transaction, figuring out which files an append just wrote, and cleaning
those files up if the commit never lands.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pyarrow as pa
from pyiceberg.catalog.noop import NoopCatalog
from pyiceberg.io import FileIO
from pyiceberg.io.pyarrow import UTC_ALIASES, schema_to_pyarrow
from pyiceberg.manifest import ManifestEntryStatus
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.snapshots import Snapshot
from pyiceberg.table.update import TableRequirement, TableUpdate

from lakana_core.errors import ValidationError
from lakana_core.storage.resolver import ResolvedStorage, root_base

logger = logging.getLogger(__name__)

_STRING_TYPE_CHECKS = (pa.types.is_string, pa.types.is_large_string)
_BINARY_TYPE_CHECKS = (pa.types.is_binary, pa.types.is_large_binary)


@dataclass(frozen=True)
class AppendResult:
    """The outcome of one `CatalogService.append` call.

    Parameters
    ----------
    rows
        Number of rows appended.
    files
        Number of data files this append wrote. `0` when `rows` is `0`.
    snapshot_id
        The id of the snapshot this append committed as the table's new
        current snapshot, or `None` when `rows` is `0` — an empty append
        commits nothing.
    """

    rows: int
    files: int
    snapshot_id: int | None


def _types_compatible(expected: pa.DataType, actual: pa.DataType) -> bool:
    """Decide whether an Arrow column's declared type matches the table's.

    Two widenings beyond strict type equality:

    - `schema_to_pyarrow` maps Iceberg's `string`/`binary` onto pyarrow's
      `large_string`/`large_binary`, but ordinary caller data — including
      pyarrow's own type inference from a plain Python list of `str` —
      produces `string`/`binary` instead. Both are accepted as the same
      Iceberg type.
    - A timestamp column's timezone is compared against pyiceberg's own
      `UTC_ALIASES` (`"UTC"`, `"+00:00"`, `"Etc/UTC"`, `"Z"`), the exact set
      `pyarrow_to_schema` itself treats as `timestamptz` — not a bare
      string-equality check against whichever alias `schema_to_pyarrow`
      happens to render (always `"UTC"`). A naive (tz-less) expected column
      still requires an exactly tz-less actual column.

    Everything else — notably integer width and, for a timestamp, its unit
    — must match exactly.

    Parameters
    ----------
    expected
        The type `schema_to_pyarrow` produced for the Iceberg column.
    actual
        The type of the caller-supplied Arrow column.

    Returns
    -------
    bool
        `True` if the two types are the same Iceberg type.
    """
    if pa.types.is_timestamp(expected) and pa.types.is_timestamp(actual):
        if expected.unit != actual.unit:
            return False
        if expected.tz in UTC_ALIASES:
            return actual.tz in UTC_ALIASES
        return actual.tz == expected.tz
    if any(is_family(expected) for is_family in _STRING_TYPE_CHECKS):
        return any(is_family(actual) for is_family in _STRING_TYPE_CHECKS)
    if any(is_family(expected) for is_family in _BINARY_TYPE_CHECKS):
        return any(is_family(actual) for is_family in _BINARY_TYPE_CHECKS)
    return expected.equals(actual)


def strip_field_metadata(data: pa.Table) -> pa.Table:
    """Strip Arrow field and schema metadata before an append is validated or written.

    pyiceberg's writer honours `PARQUET:field_id` metadata on an Arrow
    field when present, writing that column by its declared id rather than
    by name -- even though `validate_arrow_schema` (below) validates
    column identity purely by name. Caller-supplied data carrying stale or
    mismatched `field_id` metadata (for instance, forwarded from a
    different table's Arrow schema, or from a source that assigns its own
    ids) can therefore commit under the wrong column despite passing
    validation: the reviewer reproduced swapped field ids committing
    `a=99, b=1` even though both columns matched the table's schema by
    name and type. Rebuilding the schema with
    every field's metadata removed, and the table's own schema metadata
    cleared, leaves pyiceberg's writer nothing to key off but column
    order/name -- the same identity `validate_arrow_schema` already
    checked.

    Parameters
    ----------
    data
        The caller-supplied Arrow data, before validation.

    Returns
    -------
    pa.Table
        `data`, with every field's metadata removed and the table's own
        schema metadata cleared. Column names, types, nullability, and
        values are unchanged -- only metadata is dropped.

    Notes
    -----
    Built with `Table.from_arrays`, not `Table.cast`: casting to a schema
    whose field is declared non-nullable, when the actual column being cast
    contains a null, raises a bare `ValueError` from pyarrow itself --
    exactly the "null in a required column" input `validate_arrow_schema`
    (called immediately after this) is the one place meant to catch and
    turn into a clean, column-naming `ValidationError`. `from_arrays`
    attaches the existing columns to the stripped schema without
    revalidating them against it, the same way pyarrow already tolerates a
    `nullable=False` field whose array secretly contains nulls elsewhere
    (see `validate_arrow_schema`'s own docstring).
    """
    stripped_schema = pa.schema([field.remove_metadata() for field in data.schema])
    return pa.Table.from_arrays(data.columns, schema=stripped_schema).replace_schema_metadata(None)


def validate_arrow_schema(iceberg_schema: Schema, data: pa.Table) -> None:
    """Check caller-supplied Arrow data against a table's Iceberg schema.

    Parameters
    ----------
    iceberg_schema
        The table's current Iceberg schema.
    data
        The data the caller wants to append.

    Raises
    ------
    ValidationError
        If `data`'s schema has a column name repeated more than once; has a
        column the table's schema does not, or is missing one of the
        table's columns; has a column whose type does not match; declares a
        nullable Arrow column against a column the table requires to be
        non-null (the one-directional rule: a non-nullable Arrow column is
        always accepted against an optional Iceberg column, since it can
        only under-promise); or has an actual null value in a column the
        table requires to be non-null, even when the column's own Arrow
        field claims to be non-nullable — pyarrow does not itself enforce
        that a field's `nullable=False` matches its array's real contents.
        Every message names the offending column and never the data itself.
    """
    arrow_schema = data.schema
    seen_names: set[str] = set()
    for name in arrow_schema.names:
        if name in seen_names:
            raise ValidationError(f"Column {name!r} appears more than once in the data.")
        seen_names.add(name)

    expected = schema_to_pyarrow(iceberg_schema)
    expected_by_name = {field.name: field for field in expected}
    actual_by_name = {field.name: field for field in arrow_schema}

    extra = sorted(set(actual_by_name) - set(expected_by_name))
    if extra:
        raise ValidationError(f"Column(s) not in the table's schema: {', '.join(extra)}.")
    missing = sorted(set(expected_by_name) - set(actual_by_name))
    if missing:
        raise ValidationError(
            f"Missing column(s) required by the table's schema: {', '.join(missing)}."
        )

    for name, expected_field in expected_by_name.items():
        actual_field = actual_by_name[name]
        if not _types_compatible(expected_field.type, actual_field.type):
            raise ValidationError(
                f"Column {name!r} has type {actual_field.type}, but the table expects "
                f"{expected_field.type}."
            )
        if not expected_field.nullable:
            if actual_field.nullable:
                raise ValidationError(f"Column {name!r} must be non-nullable (required).")
            if data.column(name).null_count > 0:
                raise ValidationError(
                    f"Column {name!r} contains a null value, but the table requires it to "
                    "be non-null."
                )


def build_transaction(
    metadata: TableMetadata, metadata_location: str, file_io: FileIO, data: pa.Table
) -> tuple[Sequence[TableRequirement], Sequence[TableUpdate]]:
    """Stage a pyiceberg append transaction, without a real catalog behind it.

    Isolated in one place because it reaches into `Transaction`'s private
    `_requirements`/`_updates` attributes — pyiceberg exposes no public way
    to read back what `Transaction.append` staged, since the `NoopCatalog` a
    `StaticTable` is bound to always refuses `commit_transaction`.
    `CatalogService.commit_table` is what actually performs the write,
    replaying these same requirements/updates through its own governed CAS
    (the same pattern `lakana_worker.tests.test_pipeline`'s
    `_write_one_data_file_and_commit` uses).

    Parameters
    ----------
    metadata
        The table's current Iceberg metadata.
    metadata_location
        The current metadata file's location.
    file_io
        The environment's resolved `FileIO`, so the write reaches the
        correct backend (including a remote one) instead of pyiceberg's own
        scheme-guessing default (`StaticTable.from_metadata`).
    data
        Rows to append. Must already have passed `validate_arrow_schema`.

    Returns
    -------
    tuple of (sequence of TableRequirement, sequence of TableUpdate)
        What the transaction staged.
    """
    static = StaticTable(
        identifier=("lakana", metadata_location),
        metadata=metadata,
        metadata_location=metadata_location,
        io=file_io,
        catalog=NoopCatalog("lakana"),
    )
    tx = static.transaction()
    tx.append(data)
    return tx._requirements, tx._updates


def new_data_file_paths(new_snapshot: Snapshot, file_io: FileIO) -> list[str]:
    """List the data files a just-staged snapshot itself newly added.

    Filtering must happen at the manifest-**entry** level, not the manifest
    level: with `commit.manifest-merge.enabled` (a table property a caller
    can set through `create_table`, or a REST commit), pyiceberg's
    `merge_append` can fold this snapshot's new manifest together with an
    inherited one into a single merged manifest. That merged manifest's own
    `added_snapshot_id` equals the new snapshot's id even though most of its
    entries are `EXISTING` — files added by an earlier snapshot, carried
    over unchanged. A manifest-level check (`manifest.added_snapshot_id ==
    new_snapshot.snapshot_id`) would therefore both overcount
    `AppendResult.files` and, on a failed commit, delete data files this
    append never wrote (reproduced: with
    manifest merging on, the second of two appends reported `files=2`
    instead of `1`). Checking each entry's own `status` (`ADDED`, never
    `EXISTING`/`DELETED`) and its own `snapshot_id` is correct regardless of
    whether merging ever folds manifests together.

    Parameters
    ----------
    new_snapshot
        The snapshot `Transaction.append` staged (not yet committed to the
        catalog, but its manifests and manifest list are already written to
        storage).
    file_io
        Used to read the snapshot's manifest list and manifests.

    Returns
    -------
    list of str
        Absolute URIs of the data files this snapshot newly added.
    """
    paths: list[str] = []
    for manifest in new_snapshot.manifests(file_io):
        for entry in manifest.fetch_manifest_entry(file_io, discard_deleted=True):
            if (
                entry.status == ManifestEntryStatus.ADDED
                and entry.snapshot_id == new_snapshot.snapshot_id
            ):
                paths.append(entry.data_file.file_path)
    return paths


def new_manifest_paths(new_snapshot: Snapshot, file_io: FileIO) -> list[str]:
    """List the manifest files and manifest list a just-staged snapshot itself wrote.

    A manifest whose own `added_snapshot_id` equals this snapshot's id is a
    brand new physical file this transaction wrote to storage — unlike the
    per-*entry* check `new_data_file_paths` needs, this manifest-*level*
    field is exactly the right signal here: pyiceberg reuses an unaffected
    manifest's existing file verbatim, keeping its original, older
    `added_snapshot_id`, rather than rewriting it, so this never selects a
    manifest belonging to an earlier, already-committed snapshot. This holds
    even when `commit.manifest-merge.enabled` folds several manifests (some
    carrying `EXISTING` entries for older data files) into one merged
    manifest: the merged manifest is itself a new file, safe to delete on a
    failed commit — deleting it never touches the *data* files its
    `EXISTING` entries merely reference, which stay reachable through the
    manifests the table's prior, still-current snapshot already points at.
    The snapshot's own manifest list is always a fresh file, so it is always
    included.

    Parameters
    ----------
    new_snapshot
        The snapshot `Transaction.append` staged (not yet committed).
    file_io
        Used to read the snapshot's manifest list.

    Returns
    -------
    list of str
        Absolute URIs of the manifest files this snapshot newly wrote, plus
        its manifest list.
    """
    paths = [
        manifest.manifest_path
        for manifest in new_snapshot.manifests(file_io)
        if manifest.added_snapshot_id == new_snapshot.snapshot_id
    ]
    paths.append(new_snapshot.manifest_list)
    return paths


def root_relative_path(root: str, absolute: str) -> str | None:
    """Convert an absolute URI under `root` to a root-relative path.

    Parameters
    ----------
    root
        The environment's storage root.
    absolute
        An absolute URI, expected to lie under `root`.

    Returns
    -------
    str or None
        The path relative to `root`, or `None` if `absolute` does not start
        with `root` (nothing to delete through `Storage`, which only
        accepts root-relative paths).
    """
    stripped = root_base(root)
    if not absolute.startswith(stripped + "/"):
        return None
    return absolute[len(stripped) + 1 :]


def cleanup_written_paths(resolved: ResolvedStorage, paths: Iterable[str]) -> None:
    """Best-effort delete the data and manifest files a failed append wrote.

    Called from an `except` block that is about to re-raise the append's
    real failure — a lost commit race, a denial on `commit_table`'s own
    re-authorization, a metadata I/O error — so a cleanup failure here (a
    permissions error, a transient network error, the object already being
    gone) must never replace that original error. Each file is deleted
    independently; a failure on one never stops the rest from being
    attempted. Every failure is logged by its exception's class name alone —
    never the path, which can embed identifiers — and otherwise swallowed.

    Parameters
    ----------
    resolved
        The environment's resolved storage.
    paths
        Absolute URIs to delete. A path outside `resolved.config.root` (see
        `root_relative_path`) is silently skipped.
    """
    for absolute in paths:
        relative = root_relative_path(resolved.config.root, absolute)
        if relative is None:
            continue
        try:
            resolved.storage.delete(relative)
        # Deliberately broad: any failure deleting a leftover file (a permissions
        # error, a transient network error, the object already being gone) must
        # never replace the caller's original failure.
        except Exception as exc:
            logger.warning(
                "Failed to delete a file while cleaning up a failed append: %s",
                type(exc).__name__,
            )
