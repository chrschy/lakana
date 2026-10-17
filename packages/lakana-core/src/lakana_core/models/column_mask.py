"""The column mask model."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class ColumnMask(Base):
    """One column mask: a subject sees a replacement value instead of one real column.

    Same shape as `RowFilter` — exactly one of `principal_id`/`group_id`,
    always table-scoped, soft-deleted on revoke — plus `column`, the masked
    column's name, and `expression` holding the replacement value rather
    than a predicate.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this column mask.
    principal_id : uuid.UUID | None
        ID of the principal subject (mutually exclusive with group_id).
    group_id : uuid.UUID | None
        ID of the group subject (mutually exclusive with principal_id).
    environment : str
        The environment where this mask applies.
    namespace : str
        The namespace, stored unit-separator-joined.
    table : str
        The table name this mask applies to.
    environment_id : uuid.UUID or None
        The authoritative binding; the name columns are display-only.
    table_id : uuid.UUID or None
        The authoritative binding; the name columns are display-only.
    column : str
        The column name being masked.
    expression : str
        The SQL expression providing the replacement value.
    schema_id : int or None
        The id of the table's current schema when this column mask was created, or `None` for a
        column mask created before it was recorded or on a table without field ids. A rename since
        then may have made the mask cover another column
        (`lakana_core.policy.renames.renamed_policy_column`).
    created_by : uuid.UUID
        ID of the principal who created this mask.
    created_at : datetime
        Timestamp when this mask was created.
    revoked_by : uuid.UUID | None
        ID of the principal who revoked this mask, if any.
    revoked_at : datetime | None
        Timestamp when this mask was revoked, if any.
    """

    __tablename__ = "column_masks"
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
    column: Mapped[str] = mapped_column(String(255))
    expression: Mapped[str] = mapped_column(Text)
    schema_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
