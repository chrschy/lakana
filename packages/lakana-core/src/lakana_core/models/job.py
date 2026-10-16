"""The platform state of jobs: when each fires next, whether it is paused, what waits."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, String, UniqueConstraint, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime, utcnow


class JobSchedule(Base):
    """The state of one job in one environment of one repository.

    Jobs themselves are in the manifest of the project version; this row is what the platform
    adds: the next scheduled time, a pause, and at most one start that waits for the active run
    to end.
    """

    __tablename__ = "job_schedules"
    __table_args__ = (UniqueConstraint("environment_id", "repository_id", "job"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    job: Mapped[str] = mapped_column(String(255))
    next_fire_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    """When the schedule fires next; null when the job has no schedule in this environment."""
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    paused_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    pending_start: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    """The one start that waits because a run is active, or null. Keys: ``trigger``,
    ``requested_at`` (ISO 8601, UTC), ``requested_by`` (a principal id or null),
    ``scheduled_for`` (ISO 8601 or null), ``missed`` and ``merged`` (how many starts it
    stands for). Being one column, there cannot be a second waiting start. ``None`` is stored
    as SQL NULL (not the JSON text ``null``), so ``IS NULL`` finds a cleared start. An edit in
    place is not detected: assign a new dict."""
    last_fired_for: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    """The scheduled time of the last fire, to count the times missed since and to show."""


class JobFire(Base):
    """One scheduled time that fired.

    The unique key on (`job_schedule_id`, `scheduled_for`) lets a time fire only once, also
    when two scheduler leaders race over it.
    """

    __tablename__ = "job_fires"
    __table_args__ = (UniqueConstraint("job_schedule_id", "scheduled_for"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    job_schedule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_schedules.id", ondelete="CASCADE")
    )
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    """The run the fire started; null while it waits or when no run principal was bound."""
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
