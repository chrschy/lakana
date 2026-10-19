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
from lakana_core.identifiers import shown
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEventType,
    Environment,
    Secret,
    SecretUseKind,
)
from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.uses import (
    SecretUse,
    lock_entry,
    own_secret_steps,
    record_binding,
    resolve_use,
    target_row,
    used_by_its_environment,
)


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
            select(Environment).where(Environment.name == name, LIVE_ENVIRONMENT)
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
            What the secret is for; `None` leaves it unbound until `bind`. Naming it from an
            index or repository binds it only for someone who may manage secrets.

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
        ValidationError
            If `use` is an index or repository and the environment itself uses a secret of
            this name, for storage or webhooks.
        """
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(environment))
        env = self._get_environment(environment)
        if use is not None:
            use = lock_entry(self._session, use)
            if use.kind is not SecretUseKind.ENVIRONMENT and used_by_its_environment(
                self._session, env, name
            ):
                raise ValidationError(
                    f"Environment '{shown(environment)}' uses the name '{shown(name)}' for its "
                    f"storage or a webhook, so a secret of that name cannot be created for "
                    f"{use.wanted()}. Create it with --own-env, or give the index or repository "
                    "a secret under another name."
                )
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
            If the secret has been revoked, or is the environment's storage secret and `use`
            is an index or a repository.
        PermissionDenied
            If the acting principal may not manage secrets in the secret's environment.
        """
        secret = self._get_secret(secret_id)
        env = self._session.get(Environment, secret.environment_id)
        assert env is not None  # nosec B101 -- environment_id is a non-nullable FK with ON DELETE CASCADE
        scope = Securable.environment_scope(env.name)
        self._authz.authorize(p, Action.MANAGE_SECRET, scope)
        # The entry first and the secret second, the order every naming and URL change takes too.
        use = lock_entry(self._session, use)
        self._session.refresh(secret, with_for_update=True)
        if secret.revoked_at is not None:
            raise ValidationError(f"Secret {secret_id} has been revoked and cannot be bound.")
        if use.kind is not SecretUseKind.ENVIRONMENT and env.storage_secret_ref == secret.name:
            raise ValidationError(
                f"Secret '{shown(secret.name)}' is the storage secret of environment "
                f"'{shown(env.name)}', so binding it to {use.wanted()} would stop its storage. "
                f"Give {use.wanted()} a secret of its own: {own_secret_steps(env.name, use)}."
            )
        record_binding(self._session, p, secret, scope, use)
        self._session.commit()
        return secret

    def use_from_names(
        self, p: PrincipalRef, environment: str, kind: SecretUseKind, target: str | None
    ) -> SecretUse:
        """Turn a use as a caller names it into a `SecretUse`, after checking the right.

        The right is checked first, so a caller who may not manage secrets learns nothing
        about which indexes or repositories exist.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment the secret is scoped to.
        kind
            The kind of use.
        target
            The index's or repository's name; `None` for the environment.

        Returns
        -------
        SecretUse
            The use, with the target's id.

        Raises
        ------
        PermissionDenied
            If `p` may not manage secrets in `environment`.
        NotFoundError
            If no index or repository has that name.
        ValidationError
            If the environment use is given a name, or an index or repository use none.
        """
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(environment))
        return resolve_use(self._session, kind, target)

    def use_target_name(self, secret: Secret) -> str | None:
        """Name the index or repository a secret is bound to, for a listing.

        Parameters
        ----------
        secret
            The row.

        Returns
        -------
        str or None
            The name; `None` for an unbound secret, the environment use, or a target that was
            removed.
        """
        if secret.use_kind is None:
            return None
        row = target_row(self._session, secret.use_kind, secret.use_target_id)
        return row.name if row is not None else None

    def use_target_url(self, secret: Secret) -> str | None:
        """Give the URL of the index or repository a secret is bound to.

        Parameters
        ----------
        secret
            The row.

        Returns
        -------
        str or None
            The URL, which is where the secret goes; `None` for an unbound secret, the
            environment use, or a target that was removed.
        """
        if secret.use_kind is None:
            return None
        row = target_row(self._session, secret.use_kind, secret.use_target_id)
        return row.url if row is not None else None

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
