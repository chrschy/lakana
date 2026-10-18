"""The environment model."""

import hashlib
import json
import uuid

from sqlalchemy import JSON, Boolean, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, TimestampMixin


class Environment(Base, TimestampMixin):
    """An isolated deployment target such as ``dev`` or ``prod``.

    An environment owns a storage root and, from M3, a secret scope and its
    own authorization bindings. It is not part of a table's name: it is
    selected by the connection, and appears as the Iceberg REST path prefix.

    `storage_secret_ref`, when set, names a `Secret` in this same
    environment (`Secret.environment_id == self.id`,
    `Secret.name == self.storage_secret_ref`) whose decrypted key-value
    bag is merged into `storage_options` at storage-resolution time,
    secret fields winning on key collision
    (`lakana_core.storage.resolver.StorageResolver.for_environment`).
    Deliberately not a foreign key: a dangling reference is resolved (and
    fails closed with a clear `NotFoundError`) at the point storage is
    actually used, the same way an unresolvable `storage_root` scheme
    already is, rather than being enforced as a database-level constraint
    on a plain name lookup.
    """

    __tablename__ = "environments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    storage_root: Mapped[str] = mapped_column(String(1024))
    storage_options: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    storage_secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False)
    properties: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    @property
    def storage_options_digest(self) -> str:
        """Fingerprint the storage options without revealing them.

        The options can carry inline credentials, so the API never returns them; a client
        that must know whether they are exactly some map compares this digest with
        `options_digest` of that map.

        Returns
        -------
        str
            The SHA-256 of the options as sorted-key, compact JSON, in hex.
        """
        return options_digest(self.storage_options)


def options_digest(options: dict[str, str]) -> str:
    """Fingerprint a storage options map.

    Parameters
    ----------
    options
        The options.

    Returns
    -------
    str
        The SHA-256 of the options as sorted-key, compact JSON, in hex.
    """
    canonical = json.dumps(options, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
