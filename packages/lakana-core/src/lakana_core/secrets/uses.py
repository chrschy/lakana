"""What a secret may be used for, and the refusal when it is asked for anything else.

A secret has a use: a package index or a repository (by id), or its environment (storage,
webhooks and, later, tasks). An unbound secret serves its environment only. The four places
that decrypt a secret (`StorageResolver`, `lakana_worker.deploy`,
`lakana_worker.index_credentials`, `lakana_worker.notify`) call `require_use` first, so a
holder of `MANAGE_PACKAGE_INDEX` or `MANAGE_REPOSITORY` who names another secret cannot send
it to a host of their choice.
"""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown
from lakana_core.models import PackageIndex, Repository, Secret, SecretUseKind

_NOUNS = {SecretUseKind.PACKAGE_INDEX: "package index", SecretUseKind.REPOSITORY: "repository"}
_OPTIONS = {SecretUseKind.PACKAGE_INDEX: "--index", SecretUseKind.REPOSITORY: "--repository"}
_ENVIRONMENT_USE = "its environment (storage, webhooks and tasks)"


class SecretUseRefused(ValidationError):
    """A secret was asked for a use it is not bound to; the text says what to do."""


@dataclass(frozen=True)
class SecretUse:
    """One use a secret can be bound to.

    Parameters
    ----------
    kind
        What kind of use.
    target_id
        The package index's or repository's id; `None` for the environment.
    target_name
        Its name, for messages and the command; `None` for the environment.
    """

    kind: SecretUseKind
    target_id: uuid.UUID | None = None
    target_name: str | None = None

    @classmethod
    def package_index(cls, row: PackageIndex) -> SecretUse:
        """Name the use "credential of this package index".

        Parameters
        ----------
        row
            The index.

        Returns
        -------
        SecretUse
            The use.
        """
        return cls(SecretUseKind.PACKAGE_INDEX, row.id, row.name)

    @classmethod
    def repository(cls, row: Repository) -> SecretUse:
        """Name the use "credential of this repository".

        Parameters
        ----------
        row
            The repository.

        Returns
        -------
        SecretUse
            The use.
        """
        return cls(SecretUseKind.REPOSITORY, row.id, row.name)

    @classmethod
    def environment(cls) -> SecretUse:
        """Name the use "its own environment".

        Returns
        -------
        SecretUse
            The use.
        """
        return cls(SecretUseKind.ENVIRONMENT)

    def option(self) -> str:
        """Write the `lakana secret bind` option that selects this use.

        Returns
        -------
        str
            ``--index <name>``, ``--repository <name>`` or ``--own-env``.
        """
        if self.kind is SecretUseKind.ENVIRONMENT:
            return "--own-env"
        return f"{_OPTIONS[self.kind]} {shlex.quote(self.target_name or '')}"

    def wanted(self) -> str:
        """Name this use for a message.

        Returns
        -------
        str
            ``package index 'corp'``, ``repository 'analytics'`` or the environment's words.
        """
        if self.kind is SecretUseKind.ENVIRONMENT:
            return _ENVIRONMENT_USE
        return f"{_NOUNS[self.kind]} '{shown(self.target_name or '')}'"


def target_row(
    session: Session, kind: SecretUseKind, target_id: uuid.UUID | None
) -> PackageIndex | Repository | None:
    """Look up the package index or repository a binding points at.

    Parameters
    ----------
    session
        Database session.
    kind
        `PACKAGE_INDEX` or `REPOSITORY`; the environment has no row.
    target_id
        The bound id.

    Returns
    -------
    PackageIndex | Repository | None
        The row, or `None` when `target_id` is unset or names a row that was removed.
    """
    if target_id is None or kind is SecretUseKind.ENVIRONMENT:
        return None
    model = PackageIndex if kind is SecretUseKind.PACKAGE_INDEX else Repository
    return session.get(model, target_id)


def describe_use(session: Session, kind: SecretUseKind | None, target_id: uuid.UUID | None) -> str:
    """Name the use a secret is bound to, for a message.

    Parameters
    ----------
    session
        Database session, to read the target's name.
    kind
        The secret's `use_kind`.
    target_id
        The secret's `use_target_id`.

    Returns
    -------
    str
        ``no use yet``, the environment's words, ``package index 'corp'``, or ``a package index
        that was removed`` when the id names no row any more.
    """
    if kind is None:
        return "no use yet"
    if kind is SecretUseKind.ENVIRONMENT:
        return _ENVIRONMENT_USE
    row = target_row(session, kind, target_id)
    if row is None:
        return f"a {_NOUNS[kind]} that was removed"
    return f"{_NOUNS[kind]} '{shown(row.name)}'"


def bind_command(environment: str, name: str, use: SecretUse) -> str:
    """Write the command an administrator runs to bind a secret to a use.

    Parameters
    ----------
    environment
        The secret's environment.
    name
        The secret's name.
    use
        The use to bind it to.

    Returns
    -------
    str
        ``lakana secret bind <name> --env <environment> <option>``, quoted for a shell.
    """
    return f"lakana secret bind {shlex.quote(name)} --env {shlex.quote(environment)} {use.option()}"


def require_use(session: Session, secret: Secret, use: SecretUse, *, environment_name: str) -> None:
    """Refuse a secret for a use it is not bound to.

    The environment's own use takes an unbound secret too, so storage and webhooks keep
    working after the upgrade; an index or a repository takes only a secret bound to it.

    Parameters
    ----------
    session
        Database session, to name the current use in the message.
    secret
        The secret about to be decrypted.
    use
        What the caller wants it for.
    environment_name
        The secret's environment, for the message.

    Raises
    ------
    SecretUseRefused
        If the secret is unbound and `use` is an index or a repository, or bound to anything
        else; the message names the current use and the `lakana secret bind` command.
    """
    if use.kind is SecretUseKind.ENVIRONMENT:
        if secret.use_kind in (None, SecretUseKind.ENVIRONMENT):
            return
    elif (
        use.target_id is not None
        and secret.use_kind is use.kind
        and secret.use_target_id == use.target_id
    ):
        return
    command = bind_command(environment_name, secret.name, use)
    who = f"Secret '{shown(secret.name)}' of environment '{shown(environment_name)}'"
    if secret.use_kind is None:
        raise SecretUseRefused(
            f"{who} is not bound to a use yet, so Lakana does not send it to {use.wanted()}. "
            f"An administrator binds it: {command}"
        )
    current = describe_use(session, secret.use_kind, secret.use_target_id)
    raise SecretUseRefused(
        f"{who} is bound to {current}, not to {use.wanted()}, so Lakana does not use it there. "
        f"An administrator who means it changes its use: {command}"
    )
