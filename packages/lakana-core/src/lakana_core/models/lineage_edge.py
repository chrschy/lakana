"""The lineage edge model."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class LineageEdge(Base):
    """One table-level lineage edge: `source` fed a write into `target`.

    Written in the same transaction as the write it records. The table-id
    columns deliberately have no foreign key, so an edge outlives a dropped
    table (dropping a table keeps its edges for history), and the
    dotted names are recorded at write time because a dropped table's name
    can't be looked up afterwards. `query_run_id` links to the
    run that wrote the edge, where column-level lineage lives, and is
    `None` for an Arrow write or a view definition.
    """

    __tablename__ = "lineage_edges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    source_table_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    source_name: Mapped[str] = mapped_column(String(1024))
    source_snapshot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_table_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    target_name: Mapped[str] = mapped_column(String(1024))
    target_snapshot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    query_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("query_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
