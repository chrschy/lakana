# packages/lakana-core/src/lakana_core/catalog/maintenance.py
"""Table maintenance selection: which snapshots expire, which files are orphans.

Pure functions over Iceberg metadata and storage listings, so every keep
and delete rule is unit-testable. `CatalogService.maintain` applies them:
it commits the expiry through `commit_table` and deletes what
`select_orphans` selects. Orphan selection is where maintenance's deletion
is confined: only files under the table's own `data/` and
`metadata/` directories, owned by it under the "most specific owner" rule,
referenced by nothing left, never a metadata JSON file, and older than the
cutoff (`older_than_hours`, default 3 days, never below 1 hour nor above ten
years).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pyiceberg.io import FileIO
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.refs import SnapshotRefType

from lakana_core.errors import LakanaError, ValidationError
from lakana_core.storage.base import StoredObject
from lakana_core.storage.resolver import is_owned_by_location_given_nested, is_within_location

DEFAULT_ORPHAN_AGE_HOURS = 72
"""An unreferenced file younger than this is left alone (Iceberg's own default of 3 days)."""

MIN_ORPHAN_AGE_HOURS = 1
"""The floor for `older_than_hours`; anything lower could race a write."""

MAX_ORPHAN_AGE_HOURS = 87600
"""The cap for `older_than_hours`: ten years, far below what overflows a `datetime`."""

DEFAULT_KEEP_DAYS = 7
"""Snapshots younger than this many days never expire."""

DEFAULT_KEEP_LAST = 5
"""The most recent snapshots always kept."""


FAILED_SNAPSHOT_KEEP_DAYS = 7
"""How long the tag of a failed check keeps the snapshot that check saw."""

FAILED_SNAPSHOT_TAG_PREFIX = "lakana-check-failed-"
"""The start of the name of that tag; `failed_snapshot_tag` appends the run id."""

_DAY_MS = 24 * 60 * 60 * 1000


def old_failed_tags(metadata: TableMetadata, *, now_ms: int) -> list[str]:
    """Find the tags of failed checks whose seven days are over.

    A tag has no creation time of its own, so its age is the age of the snapshot it names.
    That snapshot is older than the tag by the time the checks took, which is minutes at most
    and errs on the side of removing a little early. A snapshot exactly
    `FAILED_SNAPSHOT_KEEP_DAYS` days old is not old yet, the same strictness with which
    `select_expired_snapshots` compares snapshots to its cutoff. A ref whose snapshot the
    metadata does not hold is skipped: there is nothing to age. Only a tag whose name starts
    with `FAILED_SNAPSHOT_TAG_PREFIX` is ever returned; a branch or another tag never is.

    Parameters
    ----------
    metadata
        The table's metadata.
    now_ms
        The current time in milliseconds since the epoch.

    Returns
    -------
    list of str
        The names of the old tags of failed checks, sorted.
    """
    stamps = {s.snapshot_id: s.timestamp_ms for s in metadata.snapshots}
    limit = now_ms - FAILED_SNAPSHOT_KEEP_DAYS * _DAY_MS
    return sorted(
        name
        for name, ref in metadata.refs.items()
        if ref.snapshot_ref_type == SnapshotRefType.TAG
        and name.startswith(FAILED_SNAPSHOT_TAG_PREFIX)
        and stamps.get(ref.snapshot_id, limit) < limit
    )


@dataclass(frozen=True)
class MaintenanceReport:
    """What one maintenance run did, or — for a dry run — would do.

    Parameters
    ----------
    dry_run
        Whether nothing was changed.
    snapshots_expired
        Ids of the snapshots expired.
    files_deleted
        Orphaned files deleted.
    bytes_deleted
        Their total size.
    note
        Why part of the run was skipped, if it was. Human-readable only;
        never compare it — `conflict` is the signal.
    conflict
        The table changed while maintenance ran and nothing was applied: a
        failure the caller should retry (the CLI exits 1 on it).
    tags_removed
        Names of the tags of failed checks removed because their seven days were over.
    """

    dry_run: bool
    snapshots_expired: tuple[int, ...] = field(default_factory=tuple)
    files_deleted: int = 0
    bytes_deleted: int = 0
    note: str | None = None
    conflict: bool = False
    tags_removed: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        """Render the report for the run, the audit event and the API.

        Returns
        -------
        dict
            Every field, `snapshots_expired` and `tags_removed` as lists.
        """
        return {
            "dry_run": self.dry_run,
            "snapshots_expired": list(self.snapshots_expired),
            "files_deleted": self.files_deleted,
            "bytes_deleted": self.bytes_deleted,
            "note": self.note,
            "conflict": self.conflict,
            "tags_removed": list(self.tags_removed),
        }


class MaintenanceStopped(LakanaError):
    """A maintenance run was asked to stop and did, at a point that leaves the table consistent.

    Parameters
    ----------
    report
        What the run had done by then. Expired snapshots and deleted orphans stay so, and a
        later run continues with what is left.
    """

    def __init__(self, report: MaintenanceReport) -> None:
        """Keep the report of what was done.

        Parameters
        ----------
        report
            What the run had done by then.
        """
        super().__init__("Maintenance was stopped before it finished.")
        self.report = report


def select_expired_snapshots(
    metadata: TableMetadata,
    *,
    now: datetime,
    keep_days: int,
    keep_last: int,
    old_tags: Collection[str] | None = None,
    spare: Collection[int] = (),
) -> list[int]:
    """Choose the snapshots to expire.

    Parameters
    ----------
    metadata
        The table's metadata.
    now
        The current time (timezone-aware).
    keep_days
        Snapshots younger than this many days are kept.
    keep_last
        The most recent this many snapshots are kept.
    old_tags
        The tags `old_failed_tags` names for `now`; computed here when `None`.
    spare
        Snapshots that are never expired, whatever the other rules say: the ones a task run
        still in progress needs to set its table back.

    Returns
    -------
    list of int
        Ids of snapshots older than `keep_days` that are not among the most
        recent `keep_last`, not current, and not a branch or tag head; and the
        snapshots the old tags of failed checks name, whatever `keep_days` and
        `keep_last` say, unless a ref that remains names them or one of those refs'
        heads is built on them (is a descendant). The old tags do not count as
        refs: they are about to be removed, and a failed snapshot is kept for seven days
        only. Never a snapshot in `spare`.

    Raises
    ------
    ValidationError
        If `keep_days` is negative or `keep_last` is below 1.
    """
    if keep_days < 0 or keep_last < 1:
        raise ValidationError("keep_days must be at least 0 and keep_last at least 1.")
    if old_tags is None:
        old_tags = old_failed_tags(metadata, now_ms=int(now.timestamp() * 1000))
    cutoff_ms = int((now - timedelta(days=keep_days)).timestamp() * 1000)
    newest_first = sorted(
        metadata.snapshots, key=lambda s: (s.timestamp_ms, s.sequence_number or 0), reverse=True
    )
    kept = {s.snapshot_id for s in newest_first[:keep_last]} | set(spare)
    heads = {ref.snapshot_id for name, ref in metadata.refs.items() if name not in old_tags}
    kept |= heads
    if metadata.current_snapshot_id is not None:
        kept.add(metadata.current_snapshot_id)
        heads.add(metadata.current_snapshot_id)
    expired = {s.snapshot_id for s in metadata.snapshots if s.timestamp_ms < cutoff_ms} - kept
    failed = {metadata.refs[name].snapshot_id for name in old_tags} - set(spare)
    if not failed:
        # No old tag: nothing is expired against the retention settings, so no history has
        # to be walked to protect an ancestor. This is every run of most tables.
        return sorted(expired)
    by_id = {s.snapshot_id: s for s in metadata.snapshots}
    needed: set[int] = set()
    for head in heads:
        snapshot = by_id.get(head)
        while snapshot is not None and snapshot.snapshot_id not in needed:
            needed.add(snapshot.snapshot_id)
            parent = snapshot.parent_snapshot_id
            snapshot = by_id.get(parent) if parent is not None else None
    return sorted(expired | (failed - needed))


def referenced_paths(metadata: TableMetadata, metadata_location: str, file_io: FileIO) -> set[str]:
    """Collect every file the table's remaining metadata still references.

    Parameters
    ----------
    metadata
        The metadata after expiry.
    metadata_location
        Its own file.
    file_io
        Used to read manifest lists and manifests.

    Returns
    -------
    set of str
        The metadata file, every file in its metadata log, statistics
        files, and — for every remaining snapshot — its manifest list, its
        manifests and their live data and delete files.
    """
    paths = {metadata_location}
    paths.update(entry.metadata_file for entry in metadata.metadata_log)
    paths.update(stats.statistics_path for stats in metadata.statistics)
    paths.update(stats.statistics_path for stats in metadata.partition_statistics)
    for snapshot in metadata.snapshots:
        paths.add(snapshot.manifest_list)
        for manifest in snapshot.manifests(file_io):
            paths.add(manifest.manifest_path)
            for entry in manifest.fetch_manifest_entry(file_io, discard_deleted=True):
                paths.add(entry.data_file.file_path)
    return paths


def has_foreign_reference(referenced: Iterable[str], table_location: str) -> bool:
    """Report whether any referenced path can't be matched against listings by string.

    Orphan candidates are compared to references by exact string, so a
    reference spelled differently (``file:/``, ``s3a://``, percent-encoded)
    would leave live data looking unreferenced. Maintenance fails closed.

    Parameters
    ----------
    referenced
        `referenced_paths` after expiry.
    table_location
        The table's own location.

    Returns
    -------
    bool
        `True` if any path is not canonical and within `table_location`, or
        contains a percent sign.
    """
    return any("%" in path or not is_within_location(table_location, path) for path in referenced)


def select_orphans(
    objects: Iterable[StoredObject],
    *,
    root_prefix: str,
    table_location: str,
    nested_locations: Sequence[str],
    referenced: set[str],
    cutoff: datetime,
) -> list[StoredObject]:
    """Choose which listed files are true orphans of one table.

    Parameters
    ----------
    objects
        The listing, root-relative.
    root_prefix
        The storage root with one trailing slash, to rebuild absolute URIs.
    table_location
        The table's own location.
    nested_locations
        Other tables' locations inside `table_location`
        (`nested_locations_within`); their files are never this table's.
    referenced
        `referenced_paths` after expiry.
    cutoff
        Files modified after this are kept.

    Returns
    -------
    list of StoredObject
        The orphans, in listing order.
    """
    data_prefix = f"{table_location.rstrip('/')}/data/"
    metadata_prefix = f"{table_location.rstrip('/')}/metadata/"
    orphans: list[StoredObject] = []
    for stored in objects:
        absolute = root_prefix + stored.path
        if not absolute.startswith((data_prefix, metadata_prefix)):
            continue
        if not is_owned_by_location_given_nested(table_location, absolute, nested_locations):
            continue
        if absolute.endswith(".metadata.json") or absolute in referenced:
            continue
        if stored.modified_at is None or stored.modified_at > cutoff:
            continue
        orphans.append(stored)
    return orphans


def check_orphan_age(older_than_hours: int) -> None:
    """Require `older_than_hours` to lie between the floor and the cap.

    Parameters
    ----------
    older_than_hours
        The orphan minimum age requested.

    Raises
    ------
    ValidationError
        If it is below `MIN_ORPHAN_AGE_HOURS` or above `MAX_ORPHAN_AGE_HOURS`.
    """
    if not MIN_ORPHAN_AGE_HOURS <= older_than_hours <= MAX_ORPHAN_AGE_HOURS:
        raise ValidationError(
            f"older_than_hours must be at least {MIN_ORPHAN_AGE_HOURS} and at most "
            f"{MAX_ORPHAN_AGE_HOURS}; got {older_than_hours}."
        )
