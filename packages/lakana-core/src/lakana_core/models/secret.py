"""The secret model."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class Secret(Base):
    """One envelope-encrypted secret, scoped to a single environment.

    The plaintext is never stored: `ciphertext` holds a JSON-serialized
    key-value bag encrypted under a random per-secret data key
    (AES-256-GCM); `wrapped_data_key` holds that data key wrapped by the
    deployment's master key (`lakana_core.secrets.keys.KeyProvider`);
    `nonce` is the 12-byte nonce used for the payload encryption (the
    data-key-wrapping step has its own, separately-generated nonce, kept
    inside `wrapped_data_key`'s own encoding — see
    `lakana_core.secrets.crypto`). Decrypting a `Secret` row requires the
    live `KeyProvider`; the row alone is not enough, by design.

    `format_version` says how the row was encrypted (1: no associated
    data; 2: both layers bound to the row, see
    `lakana_core.secrets.crypto`) and `key_id` names the provider and the
    key that wrapped the data key (`KeyProvider.key_id`). Rows from before
    these columns existed are format 1 with the bare key id ``env``.

    `name` is unique per `environment_id`, not globally — two different
    environments may each have a secret of the same name.

    Soft-deleted on revoke (`revoked_by`/`revoked_at`), matching every
    other model's convention: the encrypted blob is harmless at rest
    even after revocation, and the row has real audit value once the
    audit log lands.
    """

    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("environment_id", "name", name="uq_secrets_environment_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_data_key: Mapped[bytes] = mapped_column(LargeBinary)
    format_version: Mapped[int] = mapped_column(Integer)
    key_id: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
