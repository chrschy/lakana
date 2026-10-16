"""The grant model."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.auth import Action, SecurableKind
from lakana_core.grants.roles import Role
from lakana_core.models.base import Base, UTCDateTime


class Grant(Base):
    r"""One grant: a subject may exercise a role or action on a securable.

    Exactly one of `principal_id`/`group_id` is set (the subject), and
    exactly one of `role`/`action` is set (what's granted) — both enforced
    by a check constraint here and, redundantly, by `GrantService.grant`'s
    own validation, so a violation surfaces as a clean `ValidationError`
    rather than a raw constraint failure.

    `namespace` is stored unit-separator-joined, matching `Namespace.path`'s
    convention, so a grant on `gold` is findable via a prefix match against
    `gold\x1fbronze` the same way `Namespace` resolves child namespaces.

    Soft-deleted on revoke (`revoked_at`/`revoked_by`), matching
    `PersonalAccessToken`/`TaskToken`'s existing revocation pattern — a
    revoked grant's row stays, for future audit-log value.
    """

    __tablename__ = "grants"
    __table_args__ = (
        CheckConstraint(
            "(principal_id IS NULL) != (group_id IS NULL)",
            name="exactly_one_subject",
        ),
        CheckConstraint(
            "(role IS NULL) != (action IS NULL)",
            name="exactly_one_grant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True
    )
    role: Mapped[Role | None] = mapped_column(SAEnum(Role, native_enum=False), nullable=True)
    action: Mapped[Action | None] = mapped_column(SAEnum(Action, native_enum=False), nullable=True)
    kind: Mapped[SecurableKind] = mapped_column(SAEnum(SecurableKind, native_enum=False))
    environment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    namespace: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    table: Mapped[str | None] = mapped_column(String(255), nullable=True)
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    """The authoritative binding; the name columns are display-only."""
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tables.id", ondelete="SET NULL"), nullable=True, index=True
    )
    """The authoritative binding; the name columns are display-only."""
    granted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
