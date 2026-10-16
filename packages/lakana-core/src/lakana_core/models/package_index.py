"""The package index model: a private index registered once, named by URL in a lock."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, TimestampMixin


class PackageIndex(Base, TimestampMixin):
    """A package index a project's lock may name, with the secret an admin bound to it.

    Instance-wide: one row serves every environment. The credential is a
    secret *name*, resolved in the environment's secret scope when a
    project's environment is built, so dev and prod may hold different
    credentials under the same name. The row never holds a credential, and
    `url` never holds userinfo (`normalize_index_url`). Removing or changing
    an index touches no deployed project: the check before each execution
    refuses a lock whose source is no longer known.
    """

    __tablename__ = "package_indexes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    url: Mapped[str] = mapped_column(String(1024), unique=True)
    credential_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
