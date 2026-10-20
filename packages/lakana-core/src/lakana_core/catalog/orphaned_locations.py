"""Table directories that no location record names: listed on request, deleted only on request.

Releases before the location records left table directories under ``managed/`` that no record
names: tables dropped before the upgrade, creates that failed, environments deleted before
their tables became tombstones. The location sweep counts them and never deletes them
(`lakana_core.catalog.location_sweep`). `find_orphaned_locations` lists them for one
environment's storage root, and `delete_orphaned_locations` removes them when an administrator
asks.

A directory is listed when the sweep's own walk (`_Run.unrecorded_directories`) finds it, and

- it has the shape the sweep's purge accepts: canonical, strictly inside ``managed/`` of the
  root, its name ending in a table id (`_Run._confined`);
- nothing in use is it or lies inside it, in any environment (`_Run._in_use`: `LIVE` and
  `CLAIMED` records, tables, the roots derived from every table's metadata file, deleted
  environments' tables and tombstones' records included);
- no record in any state is it, lies inside it or contains it, and no table's directory contains
  it (its derived root, or for a metadata file outside ``metadata/`` the file's directory);
- it is not another environment's storage root, does not contain one, and does not lie inside
  one that lies under this ``managed/``.

Its newest file must be older than ``older_than_days`` (30 by default); a directory with a
newer file, or with a file whose age the storage does not report, is listed as recent and never
deleted.

The deletion takes the sweep's proofs rather than writing new ones. For each directory it
measures the age again and checks again, then inserts an `ABANDONED` record for the location
and commits it, which reserves the location (it is unique: a create that claimed it first
wins, and one that tries later is refused); then the sweep's purge (`_Run.purge`) deletes the
files one at a time under that record's lock, each after checking again that nothing in use and
no tombstone lies inside, and deletes the record last. A purge that cannot finish (something
appeared inside, a storage error, the time limit) deletes the record again, so the directory is
once more without one and the next call resumes it; nothing is left for the sweep to finish
unasked unless the record cannot be released, which is logged.

Known limits: the sweep's own (a process of an older release that still writes into the
directory, a namespace created inside it between the last check and a delete). A sweep that
runs during the call may see the delete's record as due and purge it under the same lock and
checks, with a `TABLE_PURGED` event of its own.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.catalog.location_sweep import (
    _COUNTED,
    _GiveUp,
    _known_locations,
    _Run,
    _Spent,
)
from lakana_core.catalog.tombstones import DEFAULT_UNDO_DAYS
from lakana_core.errors import NotFoundError
from lakana_core.identifiers import shown
from lakana_core.models import Environment, Table, TableLocation, TableLocationState
from lakana_core.storage.base import Storage
from lakana_core.storage.resolver import (
    MANAGED_PREFIX,
    is_within_location,
    table_root_from_metadata_location,
)

logger = logging.getLogger(__name__)

DEFAULT_OLDER_THAN_DAYS = 30
"""How old the newest file of a directory must be, in days, unless the caller says otherwise."""

DEFAULT_MAX_SECONDS = 20.0
"""How long one call may take: inside the SDK's 30-second request timeout."""

AUDITED_LOCATIONS = 100
"""How many deleted locations the `ORPHANED_LOCATIONS_DELETED` event names; it counts the rest."""

KEPT_RECENT = "a file in it is newer than the age asked for, or has no known age"
KEPT_EMPTY = "it holds no file any more"
KEPT_BUSY = "a table, a create in progress or a location record now uses it or a location inside it"
KEPT_WAITING = "a dropped table that can still be restored lies inside it"
KEPT_CLAIMED = "a create claimed this location while it was being checked"
KEPT_FAILED = "a storage or database error stopped it; run the command again"
KEPT_UNJUDGED = "it holds a file whose path cannot be judged; it was left alone"
KEPT_NOT_REACHED = "the time limit was reached first; run the command again to continue"

NOT_FOUND_MESSAGE = (
    "No environment named {name!r}, deleted or not. List them with `lakana env list` and "
    "`lakana env list --deleted`."
)
RELEASE_MESSAGE = (
    "The removal of directories without a record could not release its record of %s (%s); the "
    "location sweep deletes what is left of it."
)


@dataclass(frozen=True)
class Target:
    """The environment whose storage root is looked at.

    Parameters
    ----------
    id
        Its id.
    name
        Its name.
    root
        Its storage root, without a trailing slash.
    """

    id: uuid.UUID
    name: str
    root: str


@dataclass(frozen=True)
class OrphanedLocation:
    """One table directory without a location record.

    Parameters
    ----------
    location
        Its path relative to the environment's storage root.
    files
        How many files it held when it was listed.
    bytes
        Their total size.
    newest_file_at
        The modification time of its newest file; `None` when the storage reports none.
    recent
        Whether a file is newer than the age asked for, or has no known age: never deleted.
    deleted
        Whether a deletion removed it with its files.
    files_deleted
        The files a deletion removed from it.
    bytes_deleted
        Their total size.
    kept_because
        Why a deletion left it, in words; `None` in a listing and when it was deleted.
    """

    location: str
    files: int
    bytes: int
    newest_file_at: datetime | None
    recent: bool
    deleted: bool = False
    files_deleted: int = 0
    bytes_deleted: int = 0
    kept_because: str | None = None


@dataclass(frozen=True)
class OrphanedLocations:
    """What a listing or a deletion found and did.

    Parameters
    ----------
    environment
        The environment whose storage root was looked at.
    older_than_days
        The age asked for.
    locations
        The directories, in the order they were found.
    complete
        `False` when the time limit stopped the call with work left; the next call continues.
    """

    environment: str
    older_than_days: int
    locations: list[OrphanedLocation]
    complete: bool


def target(session: Session, name: str) -> Target:
    """Find an environment by name, deleted or not.

    Parameters
    ----------
    session
        Database session; rolled back.
    name
        The environment's name.

    Returns
    -------
    Target
        The environment.

    Raises
    ------
    NotFoundError
        If no environment has that name.
    """
    try:
        found = session.execute(
            select(Environment.id, Environment.storage_root).where(Environment.name == name)
        ).one_or_none()
    finally:
        session.rollback()
    if found is None:
        raise NotFoundError(NOT_FOUND_MESSAGE.format(name=name))
    return Target(id=found[0], name=name, root=found[1].rstrip("/"))


def _run(
    session: Session,
    storage: Storage,
    environment: Target,
    now: datetime,
    monotonic: Callable[[], float],
    deadline: float,
) -> _Run:
    """Build the sweep's state for one environment, with its `TABLE_PURGED` events off.

    Parameters
    ----------
    session
        Database session.
    storage
        The environment's storage.
    environment
        The environment.
    now
        The call's time.
    monotonic
        The clock the deadline is read from.
    deadline
        The monotonic time after which no further decision is taken.

    Returns
    -------
    _Run
        The state; only its walk, its checks and its purge are used.
    """
    return _Run(
        session,
        storage,
        environment.root,
        environment.name,
        now,
        timedelta(0),
        DEFAULT_UNDO_DAYS,
        monotonic,
        deadline,
        lambda: False,
        audited=False,
    )


def _ancestors(location: str) -> list[str]:
    """List every location that contains a location, nearest last.

    Parameters
    ----------
    location
        An absolute location, ``scheme://...``.

    Returns
    -------
    list of str
        Each prefix of it that ends before a ``/`` after the scheme.
    """
    start = location.index("://") + 3
    return [location[:index] for index in range(start, len(location)) if location[index] == "/"]


def _table_directories(session: Session) -> list[str]:
    """List the directory of every table in every environment, from its metadata file.

    The root `table_root_locations` derives; for a metadata file outside a ``metadata/``
    directory, which derives no root, the directory the file is in.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of str
        The directories.
    """
    query = select(Table.metadata_location).where(Table.metadata_location.is_not(None))
    return [
        table_root_from_metadata_location(path) or path.rsplit("/", 1)[0]
        for path in session.scalars(query)
        if path is not None
    ]


def _eligible(session: Session, run: _Run, location: str, environment: Target) -> bool:
    """Say whether a directory may be listed and deleted: nothing names, holds or contains it.

    Parameters
    ----------
    session
        Database session; rolled back.
    run
        The sweep's state, for its shape and in-use checks.
    location
        The directory's absolute location.
    environment
        The environment whose root is walked.

    Returns
    -------
    bool
        `True` if it has the canonical shape and nothing in any environment uses it.
    """
    if run._confined(location) is None or run._in_use(location):
        return False
    try:
        around = _ancestors(location)
        recorded = session.scalar(
            select(
                exists().where(
                    or_(
                        TableLocation.location.in_([*around, location]),
                        TableLocation.location.startswith(location + "/", autoescape=True),
                    )
                )
            )
        )
        if recorded or any(
            is_within_location(root, location) for root in _table_directories(session)
        ):
            return False
        managed = f"{environment.root}/{MANAGED_PREFIX}"
        for root in session.scalars(select(Environment.storage_root)):
            other = root.rstrip("/")
            if other == environment.root:
                continue
            if (
                other == location
                or is_within_location(location, other)
                or (is_within_location(managed, other) and is_within_location(other, location))
            ):
                return False
        return True
    finally:
        session.rollback()


def _measure(
    run: _Run, storage: Storage, relative: str, cutoff: datetime
) -> OrphanedLocation | None:
    """Count a directory's files and judge its age.

    Parameters
    ----------
    run
        The sweep's state, for the deadline.
    storage
        The environment's storage.
    relative
        The directory, relative to the root.
    cutoff
        A file modified after this makes the directory recent.

    Returns
    -------
    OrphanedLocation or None
        What it holds; `None` when it holds no file.
    """
    files = size = 0
    newest: datetime | None = None
    unknown = False
    for found in storage.list_objects(relative):
        run._tick()
        if not found.path.startswith(relative + "/"):
            continue  # a sibling that shares the name as a prefix
        files += 1
        size += found.size
        if found.modified_at is None:
            unknown = True
        elif newest is None or found.modified_at > newest:
            newest = found.modified_at
    if files == 0:
        return None
    recent = unknown or newest is None or newest > cutoff
    return OrphanedLocation(
        location=relative,
        files=files,
        bytes=size,
        newest_file_at=None if unknown else newest,
        recent=recent,
    )


def _walk(
    session: Session,
    run: _Run,
    storage: Storage,
    environment: Target,
    cutoff: datetime,
) -> tuple[list[OrphanedLocation], bool]:
    """List the directories without a record that may be deleted, and their age.

    Parameters
    ----------
    session
        Database session; rolled back.
    run
        The sweep's state, with the listing's deadline.
    storage
        The environment's storage.
    environment
        The environment.
    cutoff
        A file modified after this makes a directory recent.

    Returns
    -------
    tuple of (list of OrphanedLocation, bool)
        The directories, and whether the walk finished before the deadline.
    """
    found: list[OrphanedLocation] = []
    known = _known_locations(session)
    try:
        for relative in run.unrecorded_directories(known):
            if not _eligible(session, run, f"{environment.root}/{relative}", environment):
                continue
            entry = _measure(run, storage, relative, cutoff)
            if entry is not None:
                found.append(entry)
    except _Spent:
        return found, False
    return found, True


def find_orphaned_locations(
    session: Session,
    storage: Storage,
    *,
    environment: Target,
    now: datetime,
    older_than_days: int,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    monotonic: Callable[[], float] | None = None,
) -> OrphanedLocations:
    """List the table directories without a record under one environment's ``managed/``.

    Deletes nothing and records no audit event.

    Parameters
    ----------
    session
        Database session; left without a transaction.
    storage
        The environment's storage.
    environment
        The environment (`target`).
    now
        The call's time (the database's clock).
    older_than_days
        How old the newest file must be for a directory not to be recent.
    max_seconds
        How long the walk may take.
    monotonic
        The clock the deadline is read from; `time.monotonic` when `None`.

    Returns
    -------
    OrphanedLocations
        What was found before the deadline.
    """
    clock = monotonic or time.monotonic
    run = _run(session, storage, environment, now, clock, clock() + max_seconds)
    found, complete = _walk(
        session, run, storage, environment, now - timedelta(days=older_than_days)
    )
    return OrphanedLocations(environment.name, older_than_days, found, complete)


def delete_orphaned_locations(
    session: Session,
    storage: Storage,
    *,
    environment: Target,
    now: datetime,
    older_than_days: int,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    monotonic: Callable[[], float] | None = None,
) -> OrphanedLocations:
    """Delete the table directories without a record that are older than ``older_than_days``.

    The walk takes at most half the time; the deletions take the rest. Records no audit event:
    the caller does (`CatalogService.delete_orphaned_locations`).

    Parameters
    ----------
    session
        Database session; every step commits or rolls back, and none is left open.
    storage
        The environment's storage.
    environment
        The environment (`target`).
    now
        The call's time (the database's clock).
    older_than_days
        How old the newest file of a directory must be.
    max_seconds
        How long the call may take; it stops at the next decision past it.
    monotonic
        The clock the deadline is read from; `time.monotonic` when `None`.

    Returns
    -------
    OrphanedLocations
        Each directory found, deleted or with the reason it was kept.
    """
    clock = monotonic or time.monotonic
    start = clock()
    cutoff = now - timedelta(days=older_than_days)
    walker = _run(session, storage, environment, now, clock, start + max_seconds / 2)
    found, complete = _walk(session, walker, storage, environment, cutoff)
    run = _run(session, storage, environment, now, clock, start + max_seconds)
    results: list[OrphanedLocation] = []
    halted: str | None = None
    for entry in found:
        if entry.recent:
            results.append(replace(entry, kept_because=KEPT_RECENT))
        elif halted is not None:
            results.append(replace(entry, kept_because=halted))
        else:
            result, halted = _delete(session, run, storage, environment, entry, cutoff)
            results.append(result)
    return OrphanedLocations(
        environment.name, older_than_days, results, complete and halted is None
    )


def _delete(
    session: Session,
    run: _Run,
    storage: Storage,
    environment: Target,
    entry: OrphanedLocation,
    cutoff: datetime,
) -> tuple[OrphanedLocation, str | None]:
    """Check one directory again, reserve it with a record, and purge it as the sweep does.

    Parameters
    ----------
    session
        Database session.
    run
        The sweep's state, with the call's deadline.
    storage
        The environment's storage.
    environment
        The environment.
    entry
        The directory as the walk found it.
    cutoff
        A file modified after this makes it recent.

    Returns
    -------
    tuple of (OrphanedLocation, str or None)
        The directory, deleted or with the reason it was kept; and, when the call must stop
        (the deadline passed, or the sweep's cap of failures in a row was reached), the reason
        to give every directory after it.
    """
    location = f"{environment.root}/{entry.location}"
    try:
        run._tick()
        again = _measure(run, storage, entry.location, cutoff)
    except _Spent:
        return replace(entry, kept_because=KEPT_NOT_REACHED), KEPT_NOT_REACHED
    except Exception as exc:
        session.rollback()
        logger.warning(
            "Could not list %s again before deleting it (%s).",
            shown(entry.location),
            type(exc).__name__,
        )
        return replace(entry, kept_because=KEPT_FAILED), None
    if again is None:
        return replace(entry, kept_because=KEPT_EMPTY), None
    if again.recent:
        return replace(again, kept_because=KEPT_RECENT), None
    try:
        eligible = _eligible(session, run, location, environment)
    except Exception as exc:
        logger.warning(
            "Could not check %s again before deleting it (%s).",
            shown(entry.location),
            type(exc).__name__,
        )
        return replace(again, kept_because=KEPT_FAILED), None
    if not eligible:
        return replace(again, kept_because=KEPT_BUSY), None
    record_id = uuid.uuid4()
    try:
        session.add(
            TableLocation(
                id=record_id,
                table_id=None,
                environment_id=environment.id,
                location=location,
                state=TableLocationState.ABANDONED,
                holder_run_id=None,
                claimed_at=run._now,
                purge_after=run._now,
                revoked_policy_ids=[],
            )
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        return replace(again, kept_because=KEPT_CLAIMED), None
    before = dict(run.counts)
    halted: str | None = None
    try:
        run._guarded(record_id, run.purge)
    except _Spent:
        halted = KEPT_NOT_REACHED
    except _GiveUp:
        halted = KEPT_FAILED
    moved = {key: run.counts[key] - before[key] for key in _COUNTED}
    done = replace(
        again, files_deleted=moved["files_deleted"], bytes_deleted=moved["bytes_deleted"]
    )
    if moved["locations_purged"]:
        return replace(done, deleted=True), halted
    _release(session, record_id, entry.location)
    return replace(done, kept_because=_reason(moved, stopped=halted == KEPT_NOT_REACHED)), halted


def _reason(moved: dict[str, int], *, stopped: bool) -> str:
    """Say why a purge did not finish.

    Parameters
    ----------
    moved
        How each of the sweep's counts changed during it.
    stopped
        Whether the deadline stopped it.

    Returns
    -------
    str
        One of the ``KEPT_*`` reasons.
    """
    if moved["purges_blocked"]:
        return KEPT_BUSY
    if moved["purges_waiting"]:
        return KEPT_WAITING
    if stopped:
        return KEPT_NOT_REACHED
    if moved["left_alone"]:
        return KEPT_UNJUDGED
    return KEPT_FAILED


def _release(session: Session, record_id: uuid.UUID, relative: str) -> None:
    """Delete the record a purge that did not finish was given; never raise.

    Parameters
    ----------
    session
        Database session; committed or rolled back.
    record_id
        The record the deletion inserted.
    relative
        The location, relative to the root, for the log line.
    """
    try:
        session.execute(
            delete(TableLocation)
            .where(
                TableLocation.id == record_id,
                TableLocation.state == TableLocationState.ABANDONED,
            )
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning(RELEASE_MESSAGE, shown(relative), type(exc).__name__)
