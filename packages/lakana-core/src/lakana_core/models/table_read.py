"""The table read model."""

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class TableRead(Base):
    """How often a principal read a table on one day.

    One row per table, principal and day (UTC). The worker adds one for each table a query that
    succeeded names (`lakana_core.catalog.table_reads.record_table_reads`), and a sweep deletes the
    rows older than `LAKANA_TABLE_READS__KEEP_DAYS`. A Python task that reads through the Iceberg
    REST catalog records nothing.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this row.
    table_id : uuid.UUID
        The table; the rows go with the table.
    principal_id : uuid.UUID
        The principal whose queries read it; the rows go with the principal.
    day : datetime.date
        The UTC day of the reads.
    read_count : int
        How many succeeded queries of that principal named the table that day.
    last_read_at : datetime.datetime
        When the last of them completed.
    """

    __tablename__ = "table_reads"
    __table_args__ = (UniqueConstraint("table_id", "principal_id", "day"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date, index=True)
    read_count: Mapped[int] = mapped_column(BigInteger)
    last_read_at: Mapped[datetime] = mapped_column(UTCDateTime)
