"""Listing the data files of an Iceberg table's current snapshot, or of a named one.

This is generic Iceberg manifest-walking logic — it belongs beside
`catalog/metadata.py`, not in `lakana-worker`, so any future caller that needs a
table's current files (not only the query executor) reuses the same function
rather than re-implementing manifest traversal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pyiceberg.io import FileIO
from pyiceberg.manifest import DataFileContent
from pyiceberg.table.metadata import TableMetadata

from lakana_core.errors import CheckedSnapshotGoneError, UnsupportedTableError, ValidationError

SNAPSHOT_NOT_IN_METADATA = "The snapshot to read is no longer in the table's metadata."
"""The text of `CheckedSnapshotGoneError` when `list_data_files` is asked for a snapshot the
metadata does not hold.

It names no table, because the listing knows none. `CatalogService.load_table_at_check` looks
for the snapshot first and words the refusal with the table's name, so this one is reached only
by a caller that lists from metadata it did not get from there.
"""


@dataclass(frozen=True)
class _Refusals:
    """The texts of the three refusals of what a snapshot holds.

    Parameters
    ----------
    manifest_list
        The snapshot's manifest list lies outside the table's location.
    manifest
        One of its manifests does.
    delete_file
        It has a live row-level delete file.
    """

    manifest_list: str
    manifest: str
    delete_file: str


_OF_THE_CURRENT_SNAPSHOT = _Refusals(
    manifest_list="A table's current snapshot manifest list lies outside its location.",
    manifest="A table's manifest lies outside its location.",
    delete_file=(
        "A table's current snapshot has a live row-level delete file; "
        "Lakana's query engine does not apply deletes yet."
    ),
)
"""The refusals when the current snapshot is listed."""

_OF_A_NAMED_SNAPSHOT = _Refusals(
    manifest_list="The manifest list of the snapshot to read lies outside the table's location.",
    manifest="A manifest of the snapshot to read lies outside the table's location.",
    delete_file=(
        "The snapshot to read has a live row-level delete file; "
        "Lakana's query engine does not apply deletes yet."
    ),
)
"""The refusals when a named snapshot is listed: it need not be the current one."""


def list_data_files(
    metadata: TableMetadata,
    file_io: FileIO,
    *,
    is_valid_path: Callable[[str], bool] | None = None,
    snapshot_id: int | None = None,
) -> list[str]:
    """List the absolute paths of every live data file in a table's current snapshot.

    With `snapshot_id`, the files of that snapshot instead: what the table held when the
    snapshot was written, whether or not a branch or a tag still names it.

    Parameters
    ----------
    metadata
        The table's current Iceberg metadata.
    file_io
        Used to read the snapshot's manifest list and manifests.
    is_valid_path
        Called on the manifest-list path of the snapshot that is listed, and again on
        every manifest's own path, before either is read. `None` (the default) skips the check, for
        a caller with no table location to confine against; `lakana_worker.pipeline` always
        supplies one, built from `lakana_core.storage.resolver.is_within_location`.
        A manifest is Iceberg's own metadata, not the catalog's, so nothing
        about a data file's own path being confined (checked separately, by
        every caller of this function) stops a manifest list or a manifest
        that itself lives outside the table's location from being read in
        the first place.
    snapshot_id
        The snapshot to list. `None` (the default) lists the current snapshot. A named
        snapshot is looked up among all the metadata holds, so one that only a tag keeps is
        found, also in a table that has no current snapshot. The same path checks and the same
        refusal of delete files apply to it.

    Returns
    -------
    list of str
        Absolute URIs of every data file the listed snapshot references,
        excluding files a manifest marks as deleted. Empty if the table has no
        current snapshot (nothing has ever been written to it) and none is named.

    Raises
    ------
    CheckedSnapshotGoneError
        If `snapshot_id` is given and the metadata holds no such snapshot (maintenance removed
        it). The text names no table: `SNAPSHOT_NOT_IN_METADATA`.
    ValidationError
        If `is_valid_path` is given and rejects the manifest-list path of the
        snapshot that is listed (the current one, or the named one), or any
        manifest's own path.
    UnsupportedTableError
        If the snapshot that is listed has any live position- or equality-delete
        file. Applying deletes is out of scope for now; returning data
        files while silently ignoring the deletes that apply to them would
        return rows the writer meant removed, so this is refused outright
        rather than read wrong. A delete manifest whose entries are all
        `DELETED` (superseded, never live) does not trigger this.

    Notes
    -----
    The three refusals of what a snapshot holds are worded for the snapshot that was listed:
    the texts of the current snapshot are unchanged, and a named snapshot is never called the
    current one.
    """
    if snapshot_id is None:
        if metadata.current_snapshot_id is None:
            return []
        # `test_datafiles.py` only exercises the empty-snapshot branch above at
        # the pure-unit level (no catalog, no session); the manifest-walking
        # branch below is exercised through `lakana_worker.tests.test_pipeline`
        # instead, via `CatalogService.commit_table` against real pyiceberg
        # transactions and SQLite + a temp-directory storage root — a "unit"
        # tier test by this repo's marker convention (no testcontainers), even
        # though it is a heavier fixture than `test_datafiles.py`'s own.
        snapshot = next(
            s for s in metadata.snapshots if s.snapshot_id == metadata.current_snapshot_id
        )
    else:
        # Not through `current_snapshot_id`: a failed first build leaves a table without a
        # branch, and its snapshot is then held by a tag alone.
        named = metadata.snapshot_by_id(snapshot_id)
        if named is None:
            raise CheckedSnapshotGoneError(SNAPSHOT_NOT_IN_METADATA)
        snapshot = named
    texts = _OF_THE_CURRENT_SNAPSHOT if snapshot_id is None else _OF_A_NAMED_SNAPSHOT
    if is_valid_path is not None and not is_valid_path(snapshot.manifest_list):
        raise ValidationError(texts.manifest_list)
    paths: list[str] = []
    for manifest in snapshot.manifests(file_io):
        if is_valid_path is not None and not is_valid_path(manifest.manifest_path):
            raise ValidationError(texts.manifest)
        for entry in manifest.fetch_manifest_entry(file_io, discard_deleted=True):
            # `discard_deleted=True` already excludes `DELETED`-status
            # entries, so a delete manifest whose entries are all
            # superseded reaches here as an empty list and never trips
            # this. Checked on the entry's own `data_file.content`, not the
            # manifest's `content` field, so a delete entry is caught
            # regardless of which manifest happens to carry it.
            if entry.data_file.content != DataFileContent.DATA:
                raise UnsupportedTableError(texts.delete_file)
            paths.append(entry.data_file.file_path)
    return paths
