"""The records of managed table locations: the claim, the publish fence, and what is in use.

A location is the directory a managed table's files live in (`ResolvedStorage.table_location`,
suffixed with the table's id). The rule that lets Lakana delete such a directory unattended is
the artifact store's: **a record exists before the first file, and a directory without a record
is never deleted.**

- A create inserts and commits a `CLAIMED` record (`claim_location`) before it writes the first
  metadata or data file.
- The commit that inserts the `tables` row runs `publish_location` in the same transaction: it
  moves the record from `CLAIMED` to `LIVE`, and the commit is rolled back when no record moved
  (the publish fence). The sweep moves a claim to `ABANDONED` under the same row lock
  (`lock_location`), so exactly one of the two wins: on Postgres the second waits for the
  first's row lock and then sees its result; on SQLite the database's write lock orders them.
- A create known not to have landed releases its own claim (`release_claim`): the record is made
  `ABANDONED` first, then the files are deleted, then the record (`forget_location`), so a failed
  deletion leaves a record for the sweep and never a directory without one.

`location_in_use` is the check every deletion makes again right before it deletes: no `LIVE` or
`CLAIMED` record and no `tables` row, in any environment, may name the location or one inside it.
`dropped_inside` is the other: no `DROPPED` record (a dropped table within its undo period, or
due but not purged yet) may lie inside it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, exists, literal, or_, select, union_all, update
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.db import db_now, rowcount
from lakana_core.models import Namespace, Table, TableLocation, TableLocationState
from lakana_core.models.table_location import IN_USE_STATES, PURGEABLE_STATES
from lakana_core.storage.resolver import table_root_from_metadata_location

PROTECTED_STATES = IN_USE_STATES | {TableLocationState.DROPPED}
"""Records whose files maintenance of another table must never take for its orphans."""

CLAIM_RELEASED_MESSAGE = (
    "{table} was not created: the clean-up released the storage location this create had "
    "reserved before the table could be published, because the create's run had ended or it "
    "took longer than LAKANA_TABLES__ABANDONED_GRACE_HOURS. Nothing was published, and its "
    "files are removed. Create the table again."
)
"""The `ConflictError` of a create whose claim was abandoned before its commit (`{table}`)."""


def claim_holder(p: PrincipalRef, query_run_id: uuid.UUID | None) -> uuid.UUID | None:
    """Name the run that holds a new claim, if a run makes the create.

    Parameters
    ----------
    p
        The acting principal.
    query_run_id
        The query run writing, when the caller knows it.

    Returns
    -------
    uuid.UUID or None
        `query_run_id`; else the worker's fenced query run; else the query run or task run of
        the principal's run context; `None` for a create outside a run.
    """
    if query_run_id is not None:
        return query_run_id
    if p.write_fence is not None:
        return p.write_fence.query_run_id
    if p.run_context is not None:
        return p.run_context.query_run_id or p.run_context.task_run_id
    return None


def claim_location(
    session: Session,
    *,
    environment_id: uuid.UUID,
    location: str,
    holder_run_id: uuid.UUID | None,
) -> None:
    """Insert and commit the `CLAIMED` record of a location no file has been written to yet.

    Commits the session: the claim must be visible to the sweep before the first file exists.

    Parameters
    ----------
    session
        Database session, with nothing pending that must not commit with the claim.
    environment_id
        The environment the location belongs to.
    location
        The new table's absolute location.
    holder_run_id
        The run that makes the create, if any (`claim_holder`).
    """
    session.add(
        TableLocation(
            id=uuid.uuid4(),
            table_id=None,
            environment_id=environment_id,
            location=location,
            state=TableLocationState.CLAIMED,
            holder_run_id=holder_run_id,
            claimed_at=db_now(session),
            revoked_policy_ids=[],
        )
    )
    session.commit()


def publish_location(session: Session, *, location: str, table_id: uuid.UUID) -> bool:
    """Move a location's claim to `LIVE` for a table, inside the caller's transaction.

    The publish fence: the caller commits the `tables` row only when this returns `True`.

    Parameters
    ----------
    session
        Database session; the transaction that inserts the table.
    location
        The table's location.
    table_id
        The table.

    Returns
    -------
    bool
        `True` if a `CLAIMED` record moved; `False` if there is none (it was abandoned).
    """
    result = session.execute(
        update(TableLocation)
        .where(
            TableLocation.location == location,
            TableLocation.state == TableLocationState.CLAIMED,
        )
        .values(state=TableLocationState.LIVE, table_id=table_id)
        .execution_options(synchronize_session=False)
    )
    return rowcount(result) == 1


def lock_location(session: Session, record_id: uuid.UUID) -> TableLocation | None:
    """Take a record's lock for the rest of the transaction and read it again.

    A no-op `UPDATE` of the record: a row lock on Postgres, the database's write lock on SQLite.
    The read after it comes from the database, not from the session's identity map.

    Parameters
    ----------
    session
        Database session.
    record_id
        The record.

    Returns
    -------
    TableLocation or None
        The record as it is now, or `None` when it is gone.
    """
    locked = session.execute(
        update(TableLocation)
        .where(TableLocation.id == record_id)
        .values(state=TableLocation.state)
        .execution_options(synchronize_session=False)
    )
    if rowcount(locked) != 1:
        return None
    return session.scalars(
        select(TableLocation)
        .where(TableLocation.id == record_id)
        .execution_options(populate_existing=True)
    ).one()


def release_claim(session: Session, location: str) -> bool:
    """Mark the claim of a create known not to have landed `ABANDONED`, and commit.

    Parameters
    ----------
    session
        Database session, with no transaction the caller still needs.
    location
        The failed create's location.

    Returns
    -------
    bool
        Whether the files may be deleted: the record is `ABANDONED` (by this call or by the
        sweep before it) or there is none. `False` when a record says the location is in use.
    """
    now = db_now(session)
    session.execute(
        update(TableLocation)
        .where(
            TableLocation.location == location,
            TableLocation.state == TableLocationState.CLAIMED,
        )
        .values(state=TableLocationState.ABANDONED, purge_after=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    state = session.scalar(select(TableLocation.state).where(TableLocation.location == location))
    session.rollback()
    return state is None or state in PURGEABLE_STATES


def forget_location(session: Session, location: str) -> None:
    """Delete the abandoned record of a location whose files are gone, and commit.

    Parameters
    ----------
    session
        Database session.
    location
        The location.
    """
    session.execute(
        delete(TableLocation)
        .where(
            TableLocation.location == location,
            TableLocation.state == TableLocationState.ABANDONED,
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()


def own_table_id(session: Session, location: str) -> uuid.UUID | None:
    """Find the table whose own root location is exactly a location.

    The root is derived from the table's metadata location by
    `table_root_from_metadata_location`, as the publish fence and the backfill of migration
    `0042` derive it. A table nested inside the location (in a namespace named like its
    directory) is not its table: it lies inside, it is not it.

    Parameters
    ----------
    session
        Database session.
    location
        An absolute location.

    Returns
    -------
    uuid.UUID or None
        The table's id, or `None` when no table's root is the location.
    """
    candidates = session.execute(
        select(Table.id, Table.metadata_location).where(
            Table.metadata_location.startswith(location + "/", autoescape=True)
        )
    ).all()
    for table_id, metadata_location in candidates:
        if (
            metadata_location is not None
            and table_root_from_metadata_location(metadata_location) == location
        ):
            return table_id
    return None


def location_in_use(session: Session, location: str) -> bool:
    """Say whether anything that is in use is at a location or inside it.

    One query, in every environment: a `LIVE` or `CLAIMED` record equal to the location or below
    it, or a `tables` row whose metadata file lies below it. Locations are absolute, so a
    location of an environment on another storage root never matches.

    Parameters
    ----------
    session
        Database session.
    location
        An absolute location.

    Returns
    -------
    bool
        `True` if a deletion under `location` could reach something in use.
    """
    inside = location.rstrip("/") + "/"
    record = exists().where(
        TableLocation.state.in_(IN_USE_STATES),
        or_(
            TableLocation.location == location.rstrip("/"),
            TableLocation.location.startswith(inside, autoescape=True),
        ),
    )
    table = exists().where(Table.metadata_location.startswith(inside, autoescape=True))
    return bool(session.scalar(select(or_(record, table))))


def dropped_inside(session: Session, location: str) -> bool:
    """Say whether a dropped table that may still be restored lies inside a location.

    A `DROPPED` record strictly below the location, in any environment: its files are kept for
    its own undo period, so a purge of the location around it must wait. A nested `ABANDONED`
    record does not count: nobody can restore it, and its own purge is already due.

    Parameters
    ----------
    session
        Database session.
    location
        An absolute location about to be purged.

    Returns
    -------
    bool
        `True` if a purge of `location` would delete a tombstone's files.
    """
    inside = location.rstrip("/") + "/"
    return bool(
        session.scalar(
            select(
                exists().where(
                    TableLocation.state == TableLocationState.DROPPED,
                    TableLocation.location.startswith(inside, autoescape=True),
                )
            )
        )
    )


def other_table_locations(
    session: Session, *, environment_id: uuid.UUID, table_id: uuid.UUID, location: str
) -> list[str]:
    """List every other table's location in an environment, read in one statement.

    The locations of `LIVE`, `CLAIMED` and `DROPPED` records, and the roots derived from every
    other managed table's metadata file (a table from before records). One statement reads one
    snapshot, so a table that moves between these states at the same moment (an undrop makes a
    `DROPPED` record `LIVE` and inserts the row) is in the answer either way, never in neither.
    A location stays in one of these states until its files are gone (a purge deletes the files
    first and the record last), so the answer stays true while maintenance deletes.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    table_id
        The table being maintained, left out.
    location
        Its own location, left out.

    Returns
    -------
    list of str
        The locations; a metadata location that does not derive contributes nothing.
    """
    records = select(TableLocation.location.label("path"), literal(False).label("derive")).where(
        TableLocation.environment_id == environment_id,
        TableLocation.state.in_(PROTECTED_STATES),
        TableLocation.location != location,
    )
    rows = (
        select(Table.metadata_location.label("path"), literal(True).label("derive"))
        .join(Namespace, Table.namespace_id == Namespace.id)
        .where(
            Namespace.environment_id == environment_id,
            Table.id != table_id,
            Table.metadata_location.is_not(None),
        )
    )
    found = []
    for path, derive in session.execute(union_all(records, rows)).all():
        root = table_root_from_metadata_location(path) if derive else path
        if root is not None:
            found.append(root)
    return found


def table_root_locations(
    session: Session,
    *,
    environment_id: uuid.UUID | None = None,
    exclude: uuid.UUID | None = None,
) -> list[str]:
    """List the root locations of managed tables, derived from their metadata files.

    The check `CatalogService.maintain` makes for tables nested in the one it maintains, and the
    sweep for tables nested in a location it is about to purge.

    Parameters
    ----------
    session
        Database session.
    environment_id
        Only this environment's tables; every environment when `None`.
    exclude
        A table to leave out.

    Returns
    -------
    list of str
        Derived as the worker's own confinement check derives them; a metadata location that
        does not derive contributes nothing.
    """
    query = select(Table.metadata_location).where(Table.metadata_location.is_not(None))
    if environment_id is not None:
        query = query.join(Namespace, Table.namespace_id == Namespace.id).where(
            Namespace.environment_id == environment_id
        )
    if exclude is not None:
        query = query.where(Table.id != exclude)
    return [
        location
        for (metadata_location,) in session.execute(query).all()
        if metadata_location is not None
        and (location := table_root_from_metadata_location(metadata_location)) is not None
    ]


def abandon_claim(session: Session, record: TableLocation, *, now: datetime) -> None:
    """Move a locked claim to `ABANDONED`, purgeable from `now`, inside the caller's transaction.

    Parameters
    ----------
    session
        Database session holding the record's lock (`lock_location`).
    record
        The claim, read under the lock.
    now
        The sweep's time.
    """
    record.state = TableLocationState.ABANDONED
    record.purge_after = now
    session.flush()
