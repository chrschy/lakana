"""The table location model: the record that a managed table's directory belongs to someone."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Enum, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class TableLocationState(enum.StrEnum):
    """What a recorded table location is.

    `CLAIMED`: a create has reserved the location and may be writing files into it; no table
    names it yet. `LIVE`: the table that names it is published. `DROPPED`: the table was dropped
    and its files wait for `purge_after`; until then `undrop_table` can bring it back.
    `ABANDONED`: the create that claimed it never published; its files are removed by the sweep
    from `purge_after` on.
    """

    CLAIMED = "CLAIMED"
    LIVE = "LIVE"
    DROPPED = "DROPPED"
    ABANDONED = "ABANDONED"


IN_USE_STATES = frozenset({TableLocationState.CLAIMED, TableLocationState.LIVE})
"""A location in one of these is never deleted, nor any location that contains it."""

PURGEABLE_STATES = frozenset({TableLocationState.ABANDONED, TableLocationState.DROPPED})
"""A location in one of these is removed by the sweep once `purge_after` has passed."""


class TableLocation(Base):
    """One managed table's storage location and what may happen to its files.

    A create inserts and commits its record (`CLAIMED`) before it writes the first file, and the
    commit that inserts the `tables` row moves it to `LIVE` in the same transaction, or rolls back
    when it is no longer `CLAIMED` (the publish fence). The sweep moves a claim to `ABANDONED`
    under the same row lock, and only after its holder run has ended or, without a holder, after
    `LAKANA_TABLES__ABANDONED_GRACE_HOURS`. Files are deleted only from a location that has a
    record in a purgeable state; a directory without a record is never deleted.

    `table_id` has no foreign key, so the record outlives the row of a dropped table and an undo
    can find it again. The record goes with its environment.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this record.
    table_id : uuid.UUID or None
        The table that names the location; `None` until it is `LIVE`.
    environment_id : uuid.UUID
        The environment the location was assigned in.
    location : str
        The absolute location (the table's root directory), unique.
    state : TableLocationState
        What the location is.
    holder_run_id : uuid.UUID or None
        The query run or task run that claimed the location, if a run did.
    claimed_at : datetime.datetime
        When the location was claimed (the database's clock).
    dropped_at : datetime.datetime or None
        When the table was dropped.
    purge_after : datetime.datetime or None
        From when on the sweep removes the files of a purgeable location.
    purge_started_at : datetime.datetime or None
        When the sweep began deleting the files, set under the record's lock before the first
        delete; a dropped table can no longer be restored from then on.
    revoked_policy_ids : list
        The grants, row filters and column masks the drop revoked, as ``{"kind", "id"}``
        objects (`lakana_core.grants.revocation.RevokedPolicy`); an undrop restores exactly
        these. Empty unless `DROPPED`.
    dropped_table : dict or None
        What an undrop needs to rebuild the dropped table: its row, its column descriptions
        and its indexed columns, and its namespace path (`lakana_core.catalog.tombstones`).
        `None` unless `DROPPED`.
    """

    __tablename__ = "table_locations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    table_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    location: Mapped[str] = mapped_column(String(1024), unique=True)
    state: Mapped[TableLocationState] = mapped_column(
        Enum(TableLocationState, native_enum=False, length=16), index=True
    )
    holder_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    dropped_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    purge_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_policy_ids: Mapped[list] = mapped_column(JSON, default=list)
    dropped_table: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
