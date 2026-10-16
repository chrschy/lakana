"""The worker model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class Worker(Base):
    """A worker process that registered itself.

    What it is doing is not stored: it follows from the `RUNNING` query and deploy runs that
    carry its id and the `RUNNING` task runs whose `lease_owner` it is.
    """

    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    host: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    version: Mapped[str] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime)
    """The database's time of the worker's last heartbeat."""
    takes: Mapped[str] = mapped_column(String(16), default="all", server_default="all")
    """What the worker takes: `all`, `interactive` or `pipeline`.

    The server default is what a row inserted by code that does not set the column means.
    """
