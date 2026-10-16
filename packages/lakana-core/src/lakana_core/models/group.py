"""The group and group-membership models."""

import uuid

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, TimestampMixin


class Group(Base, TimestampMixin):
    """A named collection of principals that grants attach to.

    Instance-wide, not environment-scoped: a grant already carries
    environment scope via `Securable`, so a group does not need to
    duplicate that. Flat only — a group contains principals directly,
    never another group; there is no nested-membership resolution.
    """

    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)


class GroupMembership(Base):
    """One principal's membership in one group.

    A plain join row: no timestamp of its own, since nothing yet needs to
    know when a membership was created. Cascades on either foreign key —
    deleting a group or a principal removes the membership rows that
    reference it.
    """

    __tablename__ = "group_memberships"

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    )
