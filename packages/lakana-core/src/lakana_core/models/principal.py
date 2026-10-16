"""The principal model."""

import enum
import uuid

from sqlalchemy import Boolean, Enum, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, TimestampMixin


class PrincipalType(enum.StrEnum):
    """What kind of actor a principal represents."""

    USER = "USER"
    SERVICE_ACCOUNT = "SERVICE_ACCOUNT"


class Principal(Base, TimestampMixin):
    """An actor that can be authorized.

    M1 creates only the bootstrap admin; M3 adds real authentication, groups and credentials.
    Deactivating a principal (is_active=False) revokes its access without deleting the row,
    preserving every grant, PAT, and audit entry that references it.
    """

    __tablename__ = "principals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    type: Mapped[PrincipalType] = mapped_column(Enum(PrincipalType, native_enum=False))
    name: Mapped[str] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
