"""The row filter model."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class RowFilter(Base):
    """One row filter: a subject sees only rows matching an expression, on one table.

    Exactly one of `principal_id`/`group_id` is set (the subject) — enforced
    by a check constraint here and, redundantly, by `RowFilterService.create`'s
    own validation, so a violation surfaces as a clean `ValidationError`
    rather than a raw constraint failure.

    Always table-scoped: unlike `Grant`, there is no `kind` column, because
    a row filter's referenced columns have no defined meaning at any wider
    scope. `namespace` is stored unit-separator-joined, matching
    `Grant.namespace`'s convention.

    Soft-deleted on revoke (`revoked_at`/`revoked_by`), matching `Grant`'s
    existing revocation pattern — a revoked filter's row stays, for future
    audit-log value.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this row filter.
    principal_id : uuid.UUID | None
        ID of the principal subject (mutually exclusive with group_id).
    group_id : uuid.UUID | None
        ID of the group subject (mutually exclusive with principal_id).
    environment : str
        The environment where this filter applies.
    namespace : str
        The namespace, stored unit-separator-joined.
    table : str
        The table name this filter applies to.
    environment_id : uuid.UUID or None
        The authoritative binding; the name columns are display-only.
    table_id : uuid.UUID or None
        The authoritative binding; the name columns are display-only.
    expression : str
        The SQL expression for filtering rows.
    created_by : uuid.UUID
        ID of the principal who created this filter.
    created_at : datetime
        Timestamp when this filter was created.
    revoked_by : uuid.UUID | None
        ID of the principal who revoked this filter, if any.
    revoked_at : datetime | None
        Timestamp when this filter was revoked, if any.
    """

    __tablename__ = "row_filters"
    __table_args__ = (
        CheckConstraint(
            "(principal_id IS NULL) != (group_id IS NULL)",
            name="exactly_one_subject",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True
    )
    environment: Mapped[str] = mapped_column(String(255))
    namespace: Mapped[str] = mapped_column(String(1024))
    table: Mapped[str] = mapped_column(String(255))
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tables.id", ondelete="SET NULL"), nullable=True, index=True
    )
    expression: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
