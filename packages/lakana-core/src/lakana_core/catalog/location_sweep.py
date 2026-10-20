"""The table location sweep: expire tables, resolve claims that never published, remove files.

Run in the housekeeping thread of the worker that holds the scheduler role, next to the artifact
sweep, with the same clock (the database's), deadline, rotation and failure cap. It deletes
table data unattended, so the rules are the artifact store's:

- **A record exists before the first file** (`lakana_core.catalog.locations.claim_location`),
  so a directory without a record was not made by a create that is still running.
- **A directory without a record is never deleted.** Directories under ``managed/`` whose name
  ends in a table id and that hold files but have no record are counted
  (`unrecorded_directories`) and reported once per storage root and run: a restored database
  may have forgotten them.

What it does, per environment:

1. *Expiries* (`lakana_core.catalog.retention.expire_due`). A managed table whose retention has
   passed is dropped through the drop path, under the environment's and the table's row locks,
   after the decision is taken again with what is stored then: a tombstone with the undo period
   (at least a day), ``reason: retention``, no principal. Protected and deleted environments, a
   deployed project's assets, tables with a write queued or running, and tables whose location
   record is not their own `LIVE` one are never dropped (`expiries_postponed` counts the last
   two). At most `max_expiries` tables per run, the most overdue first.
2. *Claims.* A `CLAIMED` record is resolved only when its holder run has ended (a query run or a
   task run in a final state), or, when it has no holder or the catalog does not know the
   holder, once it is older than ``LAKANA_TABLES__ABANDONED_GRACE_HOURS``. It is resolved under
   the record's lock (`lock_location`), the lock the publish fence takes: `LIVE` when a `tables`
   row names the location (an in-doubt create whose commit landed after all), else `ABANDONED`
   with ``purge_after`` set to now and a `TABLE_LOCATION_ABANDONED` event. A create that
   publishes first is seen `LIVE` under the lock and left alone; one that publishes later
   finds no claim and rolls back.
3. *Purges.* For an `ABANDONED` or `DROPPED` record past ``purge_after``: the location must be
   an id-suffixed directory strictly inside ``managed/`` of its environment's storage root
   (`locations_refused` otherwise, record kept), and nothing in use may be the location or lie
   inside it, in any environment (`location_in_use`, and the nested-location check of
   `CatalogService.maintain`; `purges_blocked` otherwise), and no `DROPPED` record may lie inside
   it (`dropped_inside`: a dropped table nested there keeps its files for its own undo period;
   the purge waits, `purges_waiting`, and is tried again by every later sweep until that table
   is restored or purged itself). Then its files are deleted one at a
   time, each under the record's lock, after checking again that the record is still purgeable
   and that nothing in use lies inside, and only objects strictly inside the location. The
   record goes last, with a `TABLE_PURGED` event (``complete``); a pass that stops part-way
   records what it deleted with ``complete: false``, keeps the record, and the next run
   resumes. Before the first delete it marks the record ``purge_started_at`` under the lock,
   after checking again that the record is purgeable and due; an undrop of a dropped table
   takes the same lock and is refused once the mark is set.

A *deleted* environment (`lakana_core.catalog.environment_deletion`) is swept like any other,
with its storage resolved although it is hidden everywhere else, except that nothing in it
expires and a claim in it is never made `LIVE` (no table can be published into it; the claim is
abandoned once its holder ended or its grace period passed). After its purges, once its
``purge_after`` has passed and no location record of it is left in any state, its row is
deleted (`purge_environment`, ``environments_purged``, an `ENVIRONMENT_PURGED` event).

Bounds: one run has a deadline (`max_seconds`) shared between the environments, and ends at the
next decision past it or at shutdown; the starting environment and record rotate from run to
run. A storage or database error for one record is logged and counted, and after
`MAX_CONSECUTIVE_FAILURES` in a row the environment is given up for the run. Listings are
iterated, never collected.

Known limits: a create whose holder ended, or that ran past the grace period, but whose process
still writes files after its claim was abandoned (a frozen worker that wakes up) can add files to
the location after the purge; its commit is refused by the fence and those files are left as a
directory without a record, reported and never deleted. The same holds for a namespace created
inside an abandoned location between the last check and a delete. A storage delete in flight can
outlive the record's lock when the database ends the session.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.artifacts.store import delete_quietly
from lakana_core.artifacts.sweep import DEFAULT_MAX_SECONDS as ARTIFACT_SWEEP_MAX_SECONDS
from lakana_core.artifacts.sweep import MAX_CONSECUTIVE_FAILURES
from lakana_core.audit.service import AuditService
from lakana_core.auth import Securable
from lakana_core.catalog.append import root_relative_path
from lakana_core.catalog.environment_deletion import purge_environment
from lakana_core.catalog.locations import (
    abandon_claim,
    dropped_inside,
    location_in_use,
    lock_location,
    own_table_id,
    table_root_locations,
)
from lakana_core.catalog.retention import DEFAULT_MAX_EXPIRIES, expire_due
from lakana_core.catalog.tombstones import DEFAULT_UNDO_DAYS
from lakana_core.identifiers import shown
from lakana_core.models import (
    AuditEventType,
    Environment,
    QueryRun,
    QueryRunStatus,
    TableLocation,
    TableLocationState,
    TaskRun,
)
from lakana_core.models.run import TERMINAL_TASK_STATUSES
from lakana_core.models.table_location import PURGEABLE_STATES
from lakana_core.storage.base import Storage, StoredObject
from lakana_core.storage.resolver import (
    MANAGED_PREFIX,
    StorageResolver,
    is_within_location,
    nested_locations_within,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

DEFAULT_MAX_SECONDS = ARTIFACT_SWEEP_MAX_SECONDS
"""Default length of one run, the artifact sweep's; shutdown ends it earlier."""

_TERMINAL_QUERY_STATUSES = frozenset(
    {QueryRunStatus.SUCCEEDED, QueryRunStatus.FAILED, QueryRunStatus.CANCELLED}
)
_TABLE_DIRECTORY = re.compile(r"\A.+-[0-9a-f]{32}\Z", re.DOTALL)
"""The last segment of a location `ResolvedStorage.table_location` assigns."""

UNRECORDED_MESSAGE = (
    "%d table directories under managed/ in the storage of environment %s have no location "
    "record; they are left alone. If the database was restored from a backup or lost recent "
    'changes, the files are intact. See "After a restore" in the documentation before '
    "removing anything."
)
WAITING_MESSAGE = (
    "The table location sweep left %s alone for now: it waits for a dropped table inside it, "
    "which can be restored until its own undo period ends. The next sweep looks again."
)
BLOCKED_MESSAGE = (
    "The table location sweep left %s alone: a table, or a create in progress, uses this "
    "location or one inside it. Its record is kept and the next sweep looks again."
)
REFUSED_MESSAGE = (
    "The table location sweep did not purge %s: it is not a table directory under managed/ of "
    "the environment's storage root. Its record is kept and nothing in it is deleted."
)


@dataclass(frozen=True)
class LocationSweepReport:
    """What one sweep did.

    Parameters
    ----------
    tables_expired
        Tables dropped because their retention had passed.
    expiries_postponed
        Due tables kept for now: a write to them is queued or running, or their location record
        is not their own `LIVE` one; the next sweep looks again.
    claims_abandoned
        Claims moved to `ABANDONED`: their holder ended, or the grace period passed, and no table
        names their location.
    claims_published
        Claims moved to `LIVE` because a table names their location (a create in doubt landed).
    claims_kept
        Claims left alone because their holder still runs or the grace period has not passed.
    locations_purged
        Purgeable locations whose files and record are gone.
    files_deleted
        Files deleted from purgeable locations.
    bytes_deleted
        Their total size.
    purges_blocked
        Purges skipped because something in use is the location or lies inside it.
    purges_waiting
        Purges put off because a dropped table that may still be restored lies inside the
        location; tried again by the next sweep.
    locations_refused
        Purgeable records whose location is not a table directory under ``managed/``; kept.
    left_alone
        Listed objects outside the location being purged, or with a path that cannot be judged.
    unrecorded_directories
        Table directories under ``managed/`` that hold files and have no record; never deleted.
    environments_purged
        Deleted environments whose row went after their last record.
    failed_records
        Records skipped because a storage or database error stopped their step.
    failed_environments
        Environments skipped because their storage could not be resolved, or because
        `MAX_CONSECUTIVE_FAILURES` records failed in a row.
    stopped_early
        Whether the run ended at its deadline or at shutdown with work left.
    """

    tables_expired: int = 0
    expiries_postponed: int = 0
    claims_abandoned: int = 0
    claims_published: int = 0
    claims_kept: int = 0
    locations_purged: int = 0
    files_deleted: int = 0
    bytes_deleted: int = 0
    purges_blocked: int = 0
    purges_waiting: int = 0
    locations_refused: int = 0
    left_alone: int = 0
    unrecorded_directories: int = 0
    environments_purged: int = 0
    failed_records: int = 0
    failed_environments: int = 0
    stopped_early: bool = False


_COUNTED = [field.name for field in fields(LocationSweepReport) if field.type == "int"]


class _Spent(Exception):
    """The run's deadline has passed, or shutdown was asked for."""


class _GiveUp(Exception):
    """Too many records of one environment failed in a row."""


class _Blocked(Exception):
    """Something in use is the location being purged or lies inside it."""


class _Waiting(Exception):
    """A dropped table that may still be restored lies inside the location being purged."""


@dataclass
class _Tally:
    """What one purge pass deleted.

    Parameters
    ----------
    files
        Files deleted.
    size
        Their bytes.
    """

    files: int = 0
    size: int = 0


class _Run:
    """The state of one sweep over one environment.

    Parameters
    ----------
    session
        Database session; every step commits or rolls back, nothing stays open.
    storage
        The environment's storage.
    root
        The environment's storage root.
    environment
        The environment's name, for log lines and audit events.
    now
        The run's time; every age and ``purge_after`` is compared with it.
    grace
        How old a claim without a known holder must be to be resolved.
    undo_days
        ``LAKANA_TABLES__UNDO_DAYS``, for the tombstones of expired tables.
    monotonic
        The clock the deadline is read from.
    deadline
        The monotonic time after which no further decision is taken.
    should_stop
        Whether shutdown was asked for.
    deleted
        Whether the environment is deleted: no claim in it is made `LIVE`.
    """

    def __init__(
        self,
        session: Session,
        storage: Storage,
        root: str,
        environment: str,
        now: datetime,
        grace: timedelta,
        undo_days: int,
        monotonic: Callable[[], float],
        deadline: float,
        should_stop: Callable[[], bool],
        *,
        deleted: bool = False,
    ) -> None:
        self.counts: dict[str, int] = dict.fromkeys(_COUNTED, 0)
        self.stopped_early = False
        self._session = session
        self._storage = storage
        self._root = root.rstrip("/")
        self._environment = environment
        self._now = now
        self._grace = grace
        self._undo_days = undo_days
        self._monotonic = monotonic
        self._deadline = deadline
        self._should_stop = should_stop
        self._deleted = deleted
        self._failures = 0

    def _tick(self) -> None:
        """Stop the run if its deadline has passed or shutdown was asked for.

        Raises
        ------
        _Spent
            When it has.
        """
        if self._should_stop() or self._monotonic() >= self._deadline:
            self.stopped_early = True
            raise _Spent

    def _relative(self, location: str) -> str:
        """Name a location for a log line or an audit event, relative to the root when it can.

        Parameters
        ----------
        location
            An absolute location.

        Returns
        -------
        str
            The root-relative path, or the location itself when it is not under the root.
        """
        return root_relative_path(self._root, location) or location

    def environment(self, environment_id: uuid.UUID, rotate: int, expiries: int) -> None:
        """Sweep one environment: expiries first, then claims, then purges.

        Parameters
        ----------
        environment_id
            The environment.
        rotate
            Where in each list of records to start.
        expiries
            The most tables this environment may still expire in this run.

        Raises
        ------
        _Spent
            When the deadline passed.
        _GiveUp
            After too many failures in a row.
        """
        expired = expire_due(
            self._session,
            environment_id=environment_id,
            now=self._now,
            undo_days=self._undo_days,
            limit=expiries,
            tick=self._tick,
            guarded=self._guarded,
        )
        self.counts["tables_expired"] += expired.expired
        self.counts["expiries_postponed"] += expired.postponed
        claims = list(
            self._session.scalars(
                select(TableLocation.id)
                .where(
                    TableLocation.environment_id == environment_id,
                    TableLocation.state == TableLocationState.CLAIMED,
                )
                .order_by(TableLocation.claimed_at, TableLocation.id)
            )
        )
        self._session.rollback()
        for record_id in _rotated(claims, rotate):
            self._tick()
            self._guarded(record_id, self.claim)
        due = list(
            self._session.scalars(
                select(TableLocation.id)
                .where(
                    TableLocation.environment_id == environment_id,
                    TableLocation.state.in_(PURGEABLE_STATES),
                    TableLocation.purge_after <= self._now,
                )
                .order_by(TableLocation.purge_after, TableLocation.id)
            )
        )
        self._session.rollback()
        for record_id in _rotated(due, rotate):
            self._tick()
            self._guarded(record_id, self.purge)

    def _guarded(self, record_id: uuid.UUID, work: Callable[[uuid.UUID], None]) -> None:
        """Run one record's step, turning its failures into counts.

        Parameters
        ----------
        record_id
            The record.
        work
            What to do with it.

        Raises
        ------
        _Spent
            When the deadline passed.
        _GiveUp
            After `MAX_CONSECUTIVE_FAILURES` failures in a row.
        """
        try:
            work(record_id)
            self._failures = 0
        except _Spent:
            raise
        except Exception as exc:
            self._session.rollback()
            self.counts["failed_records"] += 1
            self._failures += 1
            logger.warning(
                "The table location sweep skipped a location in environment %s (%s); the next "
                "sweep tries again.",
                shown(self._environment),
                type(exc).__name__,
            )
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                raise _GiveUp from None
        finally:
            self._session.rollback()

    def _holder_ended(self, holder: uuid.UUID) -> bool | None:
        """Say whether a claim's holder run has ended.

        Parameters
        ----------
        holder
            A query run's or a task run's id.

        Returns
        -------
        bool or None
            Whether the run is in a final state; `None` when the catalog knows no such run.
        """
        status = self._session.scalar(select(QueryRun.status).where(QueryRun.id == holder))
        if status is not None:
            return status in _TERMINAL_QUERY_STATUSES
        task = self._session.scalar(select(TaskRun.status).where(TaskRun.id == holder))
        return None if task is None else task in TERMINAL_TASK_STATUSES

    def claim(self, record_id: uuid.UUID) -> None:
        """Resolve one claim, if its holder ended or its grace period passed.

        Parameters
        ----------
        record_id
            The claim.
        """
        found = self._session.execute(
            select(TableLocation.holder_run_id, TableLocation.claimed_at).where(
                TableLocation.id == record_id
            )
        ).one_or_none()
        if found is None:
            return
        holder, claimed_at = found
        ended = None if holder is None else self._holder_ended(holder)
        self._session.rollback()
        if ended is False or (ended is None and claimed_at > self._now - self._grace):
            self.counts["claims_kept"] += 1
            return
        self._tick()
        record = lock_location(self._session, record_id)
        if record is None or record.state is not TableLocationState.CLAIMED:
            return  # published, or released by its create, since it was listed
        table_id = None if self._deleted else own_table_id(self._session, record.location)
        if table_id is not None:
            record.state = TableLocationState.LIVE
            record.table_id = table_id
            self._session.commit()
            self.counts["claims_published"] += 1
            return
        abandon_claim(self._session, record, now=self._now)
        AuditService(self._session).record(
            None,
            AuditEventType.TABLE_LOCATION_ABANDONED,
            securable=Securable.environment_scope(self._environment),
            detail={
                "location": self._relative(record.location),
                "holder_run_id": None if holder is None else str(holder),
                "claimed_at": claimed_at.isoformat(),
            },
        )
        self._session.commit()
        self.counts["claims_abandoned"] += 1

    def _confined(self, location: str) -> str | None:
        """Return a location's root-relative path if it is a table directory under ``managed/``.

        Parameters
        ----------
        location
            The record's location.

        Returns
        -------
        str or None
            The path, or `None` when the location is not canonical, not strictly inside
            ``managed/`` of this environment's root, or does not end in a table id.
        """
        if not is_within_location(f"{self._root}/{MANAGED_PREFIX}", location):
            return None
        if _TABLE_DIRECTORY.match(location.rsplit("/", 1)[1]) is None:
            return None
        return root_relative_path(self._root, location)

    def _in_use(self, location: str) -> bool:
        """Say whether something in use is the location or lies inside it, in any environment.

        Parameters
        ----------
        location
            The location about to be purged.

        Returns
        -------
        bool
            `True` if it must not be touched.
        """
        try:
            if location_in_use(self._session, location):
                return True
            roots = table_root_locations(self._session)
            return location in roots or bool(nested_locations_within(location, roots))
        finally:
            self._session.rollback()

    def purge(self, record_id: uuid.UUID) -> None:
        """Delete a purgeable location's files, then its record.

        Parameters
        ----------
        record_id
            The record.

        Raises
        ------
        _Spent
            When the deadline passed; what was deleted is recorded.
        """
        found = self._session.execute(
            select(TableLocation.location, TableLocation.state).where(TableLocation.id == record_id)
        ).one_or_none()
        self._session.rollback()
        if found is None or found[1] not in PURGEABLE_STATES:
            return
        location, state = found
        relative = self._confined(location)
        if relative is None:
            self.counts["locations_refused"] += 1
            logger.warning(REFUSED_MESSAGE, shown(self._relative(location)))
            return
        if self._in_use(location):
            self._blocked(relative)
            return
        if self._waits(location):
            self._waiting(relative)
            return
        if not self._start(record_id):
            return
        tally = _Tally()
        finished = False
        try:
            if self._delete_files(record_id, location, relative, tally):
                finished = self._forget(record_id, location, state, tally)
        except _Blocked:
            self._blocked(relative)
        except _Waiting:
            self._waiting(relative)
        finally:
            if tally.files and not finished:
                self._record_purge(location, state, tally, complete=False)

    def _start(self, record_id: uuid.UUID) -> bool:
        """Mark a purge as started, under the record's lock, before its first delete.

        An undrop takes the same lock: one that came first has made the record `LIVE` (or given
        it a later ``purge_after``), and the purge does not start; one that comes later sees
        ``purge_started_at`` and is refused, so a table is never restored with files missing.

        Parameters
        ----------
        record_id
            The record.

        Returns
        -------
        bool
            Whether the record is still purgeable and due; the mark is committed then.
        """
        try:
            record = lock_location(self._session, record_id)
            if (
                record is None
                or record.state not in PURGEABLE_STATES
                or record.purge_after is None
                or record.purge_after > self._now
            ):
                return False
            if record.purge_started_at is None:
                record.purge_started_at = self._now
                self._session.commit()
            return True
        finally:
            self._session.rollback()

    def _waits(self, location: str) -> bool:
        """Say whether a dropped table that may still be restored lies inside a location.

        Parameters
        ----------
        location
            The location about to be purged.

        Returns
        -------
        bool
            `True` if the purge must wait.
        """
        try:
            return dropped_inside(self._session, location)
        finally:
            self._session.rollback()

    def _waiting(self, relative: str) -> None:
        """Count and report a purge that waits for a dropped table inside it.

        Parameters
        ----------
        relative
            The location, relative to the root.
        """
        self.counts["purges_waiting"] += 1
        logger.warning(WAITING_MESSAGE, shown(relative))

    def _blocked(self, relative: str) -> None:
        """Count and report a purge that something in use stopped.

        Parameters
        ----------
        relative
            The location, relative to the root.
        """
        self.counts["purges_blocked"] += 1
        logger.warning(BLOCKED_MESSAGE, shown(relative))

    def _delete_files(
        self, record_id: uuid.UUID, location: str, relative: str, tally: _Tally
    ) -> bool:
        """Delete every object strictly inside a location, one at a time, each under the lock.

        Parameters
        ----------
        record_id
            The record.
        location
            Its location.
        relative
            The location relative to the root.
        tally
            Counts what was deleted.

        Returns
        -------
        bool
            Whether every object the listing named inside the location was judged and deleted;
            `False` when the record stopped being purgeable or a path could not be judged.

        Raises
        ------
        _Blocked
            If something in use appeared inside the location.
        _Waiting
            If a dropped table that may still be restored lies inside the location.
        """
        complete = True
        for found in self._storage.list_objects(relative):
            self._tick()
            if not found.path.startswith(relative + "/"):
                self.counts["left_alone"] += 1  # a sibling that shares the name as a prefix
                continue
            rest = found.path[len(relative) + 1 :]
            if any(part in {"", ".", ".."} for part in rest.split("/")):
                self.counts["left_alone"] += 1
                complete = False
                continue
            if not self._delete_one(record_id, location, found, tally):
                return False
        return complete

    def _delete_one(
        self, record_id: uuid.UUID, location: str, found: StoredObject, tally: _Tally
    ) -> bool:
        """Delete one object after checking again, under the record's lock.

        Parameters
        ----------
        record_id
            The record.
        location
            Its location.
        found
            The listed object.
        tally
            Counts what was deleted.

        Returns
        -------
        bool
            `False` when the record is gone or no longer purgeable; nothing was deleted then.

        Raises
        ------
        _Blocked
            If something in use is the location or lies inside it.
        _Waiting
            If a dropped table that may still be restored lies inside the location.
        """
        try:
            record = lock_location(self._session, record_id)
            if record is None or record.state not in PURGEABLE_STATES:
                return False
            if location_in_use(self._session, location):
                raise _Blocked
            if dropped_inside(self._session, location):
                raise _Waiting
            if delete_quietly(self._storage, found.path):
                tally.files += 1
                tally.size += found.size
                self.counts["files_deleted"] += 1
                self.counts["bytes_deleted"] += found.size
            return True
        finally:
            self._session.rollback()

    def _forget(
        self, record_id: uuid.UUID, location: str, state: TableLocationState, tally: _Tally
    ) -> bool:
        """Delete a purged location's record, with its `TABLE_PURGED` event, under the lock.

        Parameters
        ----------
        record_id
            The record.
        location
            Its location.
        state
            Its state when the purge began.
        tally
            What this pass deleted.

        Returns
        -------
        bool
            Whether the record was deleted.

        Raises
        ------
        _Blocked
            If something in use is the location or lies inside it.
        _Waiting
            If a dropped table that may still be restored lies inside the location.
        """
        self._tick()
        try:
            record = lock_location(self._session, record_id)
            if record is None or record.state not in PURGEABLE_STATES:
                return False
            if location_in_use(self._session, location):
                raise _Blocked
            if dropped_inside(self._session, location):
                raise _Waiting
            self._session.delete(record)
            self._audit_purge(location, state, tally, complete=True)
            self._session.commit()
        finally:
            self._session.rollback()
        self.counts["locations_purged"] += 1
        return True

    def _audit_purge(
        self, location: str, state: TableLocationState, tally: _Tally, *, complete: bool
    ) -> None:
        """Add a `TABLE_PURGED` event to the session, uncommitted.

        Parameters
        ----------
        location
            The location.
        state
            Its state when the purge began.
        tally
            What this pass deleted.
        complete
            Whether the record is deleted with it.
        """
        AuditService(self._session).record(
            None,
            AuditEventType.TABLE_PURGED,
            securable=Securable.environment_scope(self._environment),
            detail={
                "location": self._relative(location),
                "state": state.value,
                "files": tally.files,
                "bytes": tally.size,
                "complete": complete,
            },
        )

    def _record_purge(
        self, location: str, state: TableLocationState, tally: _Tally, *, complete: bool
    ) -> None:
        """Commit a `TABLE_PURGED` event for a pass that stopped part-way, never raising.

        Parameters
        ----------
        location
            The location.
        state
            Its state when the purge began.
        tally
            What this pass deleted.
        complete
            Whether the record is gone (always `False` here).
        """
        try:
            self._session.rollback()
            self._audit_purge(location, state, tally, complete=complete)
            self._session.commit()
        except Exception as exc:
            self._session.rollback()
            logger.warning(
                "The table location sweep could not record a part-way purge (%s).",
                type(exc).__name__,
            )

    def unrecorded(self, known: set[str]) -> int:
        """Count table directories under ``managed/`` that hold files and have no record.

        Walks one level at a time and never enters a directory whose location is known or
        that ends in a table id, so a table's own files are never listed. Deletes nothing.

        Parameters
        ----------
        known
            Every recorded location and every table's location, in every environment.

        Returns
        -------
        int
            How many such directories were found before the deadline.

        Raises
        ------
        _Spent
            When the deadline passed.
        """
        count = 0
        pending = [MANAGED_PREFIX]
        while pending:
            directory = pending.pop()
            for name, is_directory in self._storage.list_children(directory):
                self._tick()
                if not is_directory:
                    continue
                path = f"{directory}/{name}"
                if f"{self._root}/{path}" in known:
                    continue
                if _TABLE_DIRECTORY.match(name) is None:
                    pending.append(path)
                elif next(iter(self._storage.list_objects(path)), None) is not None:
                    count += 1
        return count


def _rotated(ids: list[_T], rotate: int) -> list[_T]:
    """Start a list further on, wrapping round.

    Parameters
    ----------
    ids
        The items, in their order.
    rotate
        How far on to start.

    Returns
    -------
    list
        The same items.
    """
    if not ids:
        return ids
    start = rotate % len(ids)
    return ids[start:] + ids[:start]


def _known_locations(session: Session) -> set[str]:
    """Read every recorded location and every table's location, in every environment.

    Parameters
    ----------
    session
        Database session; rolled back.

    Returns
    -------
    set of str
        The locations.
    """
    try:
        recorded = set(session.scalars(select(TableLocation.location)))
        return recorded | set(table_root_locations(session))
    finally:
        session.rollback()


def sweep_table_locations(
    session: Session,
    storage: StorageResolver,
    *,
    now: datetime,
    grace_hours: int,
    undo_days: int = DEFAULT_UNDO_DAYS,
    max_expiries: int = DEFAULT_MAX_EXPIRIES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    monotonic: Callable[[], float] | None = None,
    rotation: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> LocationSweepReport:
    """Expire due tables, resolve old claims and purge what is due, in every environment, once.

    Parameters
    ----------
    session
        Database session; the sweep commits its own transactions on it and leaves it without one
        open.
    storage
        Resolves each environment's storage.
    now
        The sweep's time (the database's clock in production), used for the whole run.
    grace_hours
        `LAKANA_TABLES__ABANDONED_GRACE_HOURS`: how old a claim with no known holder run must be
        before it is resolved.
    undo_days
        `LAKANA_TABLES__UNDO_DAYS`: how long an expired table can be restored (at least a day).
    max_expiries
        `LAKANA_TABLES__MAX_EXPIRIES_PER_SWEEP`: the most tables the run expires, in every
        environment together.
    max_seconds
        How long the run may take, shared between the environments, before it stops at the next
        decision.
    monotonic
        The clock the deadline is read from; `time.monotonic` when `None`.
    rotation
        A number that grows by one with each run of the same process; it moves the starting
        environment and record.
    should_stop
        Called before every decision; the run ends when it returns `True` (shutdown).

    Returns
    -------
    LocationSweepReport
        What was done. A record that failed is counted and skipped; an environment whose storage
        cannot be resolved is counted and skipped; the others are still swept.
    """
    clock = monotonic or time.monotonic
    stop = should_stop or (lambda: False)
    final = clock() + max_seconds
    grace = timedelta(hours=grace_hours)
    totals = dict.fromkeys(_COUNTED, 0)
    stopped = False
    environments = list(
        session.execute(
            select(
                Environment.id,
                Environment.name,
                Environment.storage_root,
                Environment.deleted_at.is_not(None),
            ).order_by(Environment.name)
        ).all()
    )
    session.rollback()
    walked: dict[str, int] = defaultdict(int)
    known: set[str] | None = None
    for position, (environment_id, name, root, deleted) in enumerate(
        _rotated(environments, rotation)
    ):
        moment = clock()
        if stop() or moment >= final:
            stopped = True
            break
        share = moment + (final - moment) / (len(environments) - position)
        try:
            resolved = storage.for_environment(name, include_deleted=True)
            run = _Run(
                session,
                resolved.storage,
                root,
                name,
                now,
                grace,
                undo_days,
                clock,
                share,
                stop,
                deleted=bool(deleted),
            )
            try:
                run.environment(environment_id, rotation, max_expiries - totals["tables_expired"])
                key = root.rstrip("/")
                if key not in walked:
                    known = _known_locations(session) if known is None else known
                    walked[key] = run.unrecorded(known)
                    if walked[key]:
                        logger.warning(UNRECORDED_MESSAGE, walked[key], shown(name))
                    totals["unrecorded_directories"] += walked[key]
                if deleted and purge_environment(session, environment_id, now=now):
                    run.counts["environments_purged"] += 1
            finally:
                for key_name, value in run.counts.items():
                    totals[key_name] += value
                stopped = stopped or run.stopped_early
        except _Spent:
            continue
        except Exception as exc:
            totals["failed_environments"] += 1
            session.rollback()
            logger.warning(
                "The table location sweep skipped environment %s (%s).",
                shown(name),
                type(exc).__name__ if not isinstance(exc, _GiveUp) else "repeated failures",
            )
    report = LocationSweepReport(**totals, stopped_early=stopped)
    quiet = {"left_alone", "unrecorded_directories", "claims_kept"}
    if any(getattr(report, key) for key in totals if key not in quiet):
        logger.info(
            "Table location sweep: %d tables expired, %d expiries postponed, %d claims abandoned, "
            "%d claims found published, %d locations purged (%d files, %d bytes), %d deleted "
            "environments removed; %d claims "
            "kept, %d purges blocked, %d purges waiting, %d locations refused, %d objects left "
            "alone, %d records and %d environments failed%s.",
            report.tables_expired,
            report.expiries_postponed,
            report.claims_abandoned,
            report.claims_published,
            report.locations_purged,
            report.files_deleted,
            report.bytes_deleted,
            report.environments_purged,
            report.claims_kept,
            report.purges_blocked,
            report.purges_waiting,
            report.locations_refused,
            report.left_alone,
            report.failed_records,
            report.failed_environments,
            "; stopped at its time limit" if stopped else "",
        )
    return report
