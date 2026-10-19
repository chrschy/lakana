"""The table column model."""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lakana_core.models.base import Base
from lakana_core.models.table import Table


class TableColumn(Base):
    """One column of a table, view or external table, kept so that a search can find it.

    Written by `lakana_core.catalog.columns.sync_table_columns` on every schema change (a create,
    a commit, an external table, a view), and filled for older tables by the worker's
    housekeeping sweep. The table's own metadata stays the authority; this is an index of it.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this row.
    table_id : uuid.UUID
        The table; the rows go with the table.
    name : str
        The column's name.
    type : str
        The column's type as the API spells it (``long``, ``string``, ``decimal(10,2)``, ...).
    position : int
        The column's place in the table's schema, from 0.
    """

    __tablename__ = "table_columns"
    __table_args__ = (UniqueConstraint("table_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(1024), index=True)
    type: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)

    table: Mapped[Table] = relationship()
    """The table. Declared so that the unit of work inserts a table before its columns
    when both are new in one commit."""
