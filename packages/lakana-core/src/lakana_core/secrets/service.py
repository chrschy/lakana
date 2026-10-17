"""Secret creation, listing, rotation, binding, and revocation.

Every method here authorizes `Action.MANAGE_SECRET` before doing anything
else it can (`create`/`list_for_environment`, which already know the
target environment from their own parameters) or as soon as it can
(`rotate`/`revoke`, which must look up the row first to learn its
environment). This service never decrypts a secret's value — see
`lakana_core.secrets.crypto` for why, and `lakana_core.storage.resolver` and
`lakana_worker.deploy` and `lakana_worker.index_credentials` for the places decryption
actually happens.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import AlreadyExistsError, NotFoundError, ValidationError
from lakana_core.models import AuditEventType, Environment, Secret
from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.uses import SecretUse, first_use, record_binding


class SecretService:
    """Create, list, rotate, and revoke secrets.

    Parameters
    ----------
    session
        Database session. Each mutating method commits internally.
    authz
        Authorization service consulted before every operation.
    key_provider
        Wraps each secret's freshly generated data key. Never used to
        decrypt anything here — only `encrypt_secret_values` is called
        from this class.
    """

    def __init__(
        self, session: Session, authz: AuthorizationService, key_provider: KeyProvider
    ) -> None:
        self._session = session
        self._authz = authz
        self._key_provider = key_provider

    def _get_environment(self, name: str) -> Environment:
        """Look up an environment by name.

        Parameters
        ----------
        name
            Environment name.

        Returns
        -------
        Environment
            The matching row.

        Raises
        ------
        NotFoundError
            If no environment has that name.
        """
        env = self._session.execute(
            select(Environment).where(Environment.name == name)
        ).scalar_one_or_none()
        if env is None:
            raise NotFoundError(f"No environment named {name!r}.")
        return env

    def _get_secret(self, secret_id: uuid.UUID) -> Secret:
        """Look up a secret by id, without authorizing.

        Parameters
        ----------
        secret_id
            The secret's id.

        Returns
        -------
        Secret
            The matching row.

        Raises
        ------
        NotFoundError
            If no secret has that id.
        """
        secret = self._session.get(Secret, secret_id)
        if secret is None:
            raise NotFoundError(f"No secret {secret_id}.")
        return secret

    def create(
        self,
        p: PrincipalRef,
        *,
        environment: str,
        name: str,
        values: dict[str, str],
        use: SecretUse | None = None,
    ) -> Secret:
        """Create a secret.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment this secret is scoped to.
        name
            The secret's name, unique within `environment`.
        values
            The plaintext key-value bag to encrypt and store.
        use
            What the secret is for; `None` binds it on first use: to the one package index or
            repository that names it, else it stays unbound (`first_use`).

        Returns
        -------
        Secret
            The created row. Its `ciphertext`/`nonce`/`wrapped_data_key`
            fields are the encrypted form; nothing about `values` is
            otherwise retrievable from the return value.

        Raises
        ------
        NotFoundError
            If no environment named `environment` exists.
        AlreadyExistsError
            If a secret named `name` already exists in `environment`.
        LakanaError
            If no usable secrets master key is configured.
        PermissionDenied
            If the acting principal may not manage secrets in this
            environment.
        """
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(environment))
        env = self._get_environment(environment)
        if use is None:
            use = first_use(self._session, name)
        secret_id = uuid.uuid4()
        encrypted = encrypt_secret_values(
            values, self._key_provider, SecretBinding(secret_id, env.id, name)
        )
        secret = Secret(
            id=secret_id,
            environment_id=env.id,
            name=name,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            wrapped_data_key=encrypted.wrapped_data_key,
            format_version=encrypted.format_version,
            key_id=encrypted.key_id,
            created_by=p.id,
            created_at=datetime.now(UTC),
        )
        AuditService(self._session).record(
            p.id,
            AuditEventType.SECRET_CREATED,
            securable=Securable.environment_scope(environment),
            detail={"name": name, "use": use.kind.value if use is not None else None},
        )
        if use is not None:
            record_binding(self._session, p, secret, Securable.environment_scope(environment), use)
        self._session.add(secret)
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            # A fixed message, not `str(exc)`: SQLAlchemy's `IntegrityError`
            # string embeds the failed statement and its bound parameters,
            # which must never reach a caller.
            raise AlreadyExistsError(
                f"A secret named {name!r} already exists in environment {environment!r}."
            ) from exc
        return secret

    def bind(self, p: PrincipalRef, secret_id: uuid.UUID, use: SecretUse) -> Secret:
        """Bind a secret to a use, whatever it was bound to before.

        Parameters
        ----------
        p
            The acting principal.
        secret_id
            The secret's id.
        use
            The use (`resolve_use` builds it from names).

        Returns
        -------
        Secret
            The updated row.

        Raises
        ------
        NotFoundError
            If no secret has that id.
        ValidationError
            If the secret has been revoked.
        PermissionDenied
            If the acting principal may not manage secrets in the secret's environment.
        """
        secret = self._get_secret(secret_id)
        env = self._session.get(Environment, secret.environment_id)
        assert env is not None  # nosec B101 -- environment_id is a non-nullable FK with ON DELETE CASCADE
        scope = Securable.environment_scope(env.name)
        self._authz.authorize(p, Action.MANAGE_SECRET, scope)
        if secret.revoked_at is not None:
            raise ValidationError(f"Secret {secret_id} has been revoked and cannot be bound.")
        record_binding(self._session, p, secret, scope, use)
        self._session.commit()
        return secret

    def list_for_environment(self, p: PrincipalRef, environment: str) -> list[Secret]:
        """List an environment's active secrets.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment whose secrets to list.

        Returns
        -------
        list of Secret
            Every active secret in `environment`. Each row's encrypted
            fields are present (they are the row), but nothing decrypts
            them here or anywhere downstream of this method.

        Raises
        ------
        NotFoundError
            If no environment has that name.
        PermissionDenied
            If the acting principal may not manage secrets in this
            environment.
        """
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(environment))
        env = self._get_environment(environment)
        return list(
            self._session.execute(
                select(Secret).where(Secret.environment_id == env.id, Secret.revoked_at.is_(None))
            )
            .scalars()
            .all()
        )

    def rotate(self, p: PrincipalRef, secret_id: uuid.UUID, *, values: dict[str, str]) -> Secret:
        """Re-encrypt a secret's value under a freshly generated data key.

        The secret is written in the current stored format, whatever format
        it was in: this is how a secret stored in an older format is
        upgraded.

        The secret's id and name are unchanged, so anything referencing it
        by name (an environment's `storage_secret_ref`) picks up the new
        value on next use, with no reference update needed anywhere.

        Parameters
        ----------
        p
            The acting principal.
        secret_id
            The secret's id.
        values
            The new plaintext key-value bag.

        Returns
        -------
        Secret
            The updated row.

        Raises
        ------
        NotFoundError
            If no secret has that id.
        ValidationError
            If the secret has been revoked.
        LakanaError
            If no usable secrets master key is configured.
        PermissionDenied
            If the acting principal may not manage secrets in this
            secret's environment.
        """
        secret = self._get_secret(secret_id)
        env = self._session.get(Environment, secret.environment_id)
        assert env is not None  # nosec B101 -- environment_id is a non-nullable FK with ON DELETE CASCADE
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(env.name))
        if secret.revoked_at is not None:
            raise ValidationError(f"Secret {secret_id} has been revoked and cannot be rotated.")
        encrypted = encrypt_secret_values(
            values, self._key_provider, SecretBinding(secret.id, secret.environment_id, secret.name)
        )
        AuditService(self._session).record(
            p.id,
            AuditEventType.SECRET_ROTATED,
            securable=Securable.environment_scope(env.name),
            detail={"name": secret.name},
        )
        secret.ciphertext = encrypted.ciphertext
        secret.nonce = encrypted.nonce
        secret.wrapped_data_key = encrypted.wrapped_data_key
        secret.format_version = encrypted.format_version
        secret.key_id = encrypted.key_id
        self._session.commit()
        return secret

    def revoke(self, p: PrincipalRef, secret_id: uuid.UUID) -> None:
        """Revoke a secret.

        Idempotent: revoking an already-revoked secret is a no-op — it
        leaves the existing `revoked_at`/`revoked_by` untouched and records
        no second `SECRET_REVOKED` event, matching PATs, task tokens, row
        filters, and column masks.

        Parameters
        ----------
        p
            The acting principal.
        secret_id
            The secret's id.

        Raises
        ------
        NotFoundError
            If no secret has that id.
        PermissionDenied
            If the acting principal may not manage secrets in this
            secret's environment.
        """
        secret = self._get_secret(secret_id)
        env = self._session.get(Environment, secret.environment_id)
        assert env is not None  # nosec B101 -- environment_id is a non-nullable FK with ON DELETE CASCADE
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(env.name))
        if secret.revoked_at is not None:
            return
        AuditService(self._session).record(
            p.id,
            AuditEventType.SECRET_REVOKED,
            securable=Securable.environment_scope(env.name),
            detail={"name": secret.name},
        )
        secret.revoked_at = datetime.now(UTC)
        secret.revoked_by = p.id
        self._session.commit()
