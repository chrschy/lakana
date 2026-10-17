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

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import shown
from lakana_core.models import (
    AuditEventType,
    Environment,
    JobVersion,
    NotificationDelivery,
    PackageIndex,
    Repository,
    Secret,
    SecretUseKind,
)

_NOUNS = {SecretUseKind.PACKAGE_INDEX: "package index", SecretUseKind.REPOSITORY: "repository"}
_OPTIONS = {SecretUseKind.PACKAGE_INDEX: "--index", SecretUseKind.REPOSITORY: "--repository"}
_ENVIRONMENT_USE = "its environment (storage, webhooks and tasks)"


def _text(name: str) -> str:
    """Make a name safe to print without changing a name a shell can carry.

    Parameters
    ----------
    name
        A secret, environment, index or repository name.

    Returns
    -------
    str
        The name as it is when every character is printable (so a pasted command still matches,
        also for a long or non-ASCII name); otherwise its escaped form from `shown`.
    """
    return name if name.isprintable() else shown(name)


class SecretUseRefused(ValidationError):
    """A secret was asked for a use it is not bound to; the text says what to do."""


class SecretBindingRefused(PermissionDenied):
    """Naming a secret, or moving its host, needs a right the principal lacks.

    Parameters
    ----------
    message
        What happened and what to do next.
    scope
        The environment the secret lives in.
    reason
        The reason recorded with the denial in the audit log.
    """

    def __init__(self, message: str, scope: Securable, reason: str) -> None:
        super().__init__(message)
        self.scope = scope
        self.reason = reason


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

    def __post_init__(self) -> None:
        """Refuse an index or repository use that names no target.

        Raises
        ------
        ValueError
            If the kind is not the environment and `target_id` or `target_name` is missing, so
            that no message or command ever names an empty target.
        """
        if self.kind is not SecretUseKind.ENVIRONMENT and (
            self.target_id is None or self.target_name is None
        ):
            raise ValueError(f"A {self.kind.value} use needs the target's id and name.")

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
        return f"{_OPTIONS[self.kind]} {shlex.quote(_text(self.target_name or ''))}"

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
    return (
        f"lakana secret bind {shlex.quote(_text(name))} --env {shlex.quote(_text(environment))} "
        f"{use.option()}"
    )


def _bound_to(secret: Secret, use: SecretUse) -> bool:
    """Say whether a secret is bound to exactly this use.

    Parameters
    ----------
    secret
        The secret.
    use
        The use.

    Returns
    -------
    bool
        Whether kind and target match.
    """
    return secret.use_kind is use.kind and secret.use_target_id == use.target_id


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
        The secret's environment, for the message; it must be the one the secret belongs to.

    Raises
    ------
    ValueError
        If `environment_name` is not the name of the secret's own environment: the caller
        loaded the secret from one environment and named another, which is a bug.
    SecretUseRefused
        If the secret is unbound and `use` is an index or a repository, or bound to anything
        else; the message names the current use and the `lakana secret bind` command.
    """
    owner = session.get(Environment, secret.environment_id)
    if owner is None or owner.name != environment_name:
        raise ValueError(
            f"Secret '{shown(secret.name)}' does not belong to environment "
            f"'{shown(environment_name)}'."
        )
    if use.kind is SecretUseKind.ENVIRONMENT:
        if secret.use_kind in (None, SecretUseKind.ENVIRONMENT):
            return
    elif use.target_id is not None and _bound_to(secret, use):
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


def record_binding(
    session: Session, p: PrincipalRef, secret: Secret, scope: Securable, use: SecretUse
) -> None:
    """Bind a secret to a use and add the audit event; the caller commits.

    Parameters
    ----------
    session
        Database session.
    p
        The acting principal.
    secret
        The secret to bind; its previous use goes into the event.
    scope
        The secret's environment scope.
    use
        The use to bind it to.
    """
    AuditService(session).record(
        p.id,
        AuditEventType.SECRET_BOUND,
        securable=scope,
        detail={
            "name": secret.name,
            "use": use.kind.value,
            "target": use.target_name,
            "previous": secret.use_kind.value if secret.use_kind is not None else None,
        },
    )
    secret.use_kind, secret.use_target_id = use.kind, use.target_id


def first_use(session: Session, name: str) -> SecretUse | None:
    """Find the use a new secret takes on first use: the one entry that names it.

    Parameters
    ----------
    session
        Database session.
    name
        The secret's name.

    Returns
    -------
    SecretUse or None
        The package index or repository when exactly one of all of them names `name` as its
        credential; `None` otherwise (none, or more than one: an administrator decides).
    """
    indexes = (
        session.execute(select(PackageIndex).where(PackageIndex.credential_secret == name))
        .scalars()
        .all()
    )
    repositories = (
        session.execute(select(Repository).where(Repository.credential_secret == name))
        .scalars()
        .all()
    )
    if len(indexes) + len(repositories) != 1:
        return None
    if indexes:
        return SecretUse.package_index(indexes[0])
    return SecretUse.repository(repositories[0])


def resolve_use(session: Session, kind: SecretUseKind, target: str | None) -> SecretUse:
    """Turn a use as a caller names it into a `SecretUse`.

    Parameters
    ----------
    session
        Database session.
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
    ValidationError
        If the environment use is given a name, or an index or repository use none.
    NotFoundError
        If no index or repository has that name.
    """
    if kind is SecretUseKind.ENVIRONMENT:
        if target is not None:
            raise ValidationError(
                "The environment use takes no name. Leave the name out to bind the secret to "
                "its environment."
            )
        return SecretUse.environment()
    noun = _NOUNS[kind]
    listing = "lakana index list" if kind is SecretUseKind.PACKAGE_INDEX else "lakana repo list"
    if not target:
        raise ValidationError(f"Name the {noun} the secret is for; `{listing}` shows them.")
    model = PackageIndex if kind is SecretUseKind.PACKAGE_INDEX else Repository
    row = session.execute(select(model).where(model.name == target)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            f"No {noun} named '{_text(target)}'; `{listing}` shows the registered ones."
        )
    return SecretUse(kind, row.id, row.name)


def record_refusal(
    authz: AuthorizationService, p: PrincipalRef, refusal: SecretBindingRefused
) -> None:
    """Record a refused binding as `AUTHORIZATION_DENIED`, after the caller's rollback.

    `authorize` cannot be used for these refusals: it commits, and by then the bindings made
    before the refusal are pending in the same transaction.

    Parameters
    ----------
    authz
        The authorization service; only an auditing one records.
    p
        The refused principal.
    refusal
        The refusal, which carries the environment scope and the reason.
    """
    if isinstance(authz, AuditingAuthorization):
        authz.record_denial(p, Action.MANAGE_SECRET, refusal.scope, reason=refusal.reason)


def used_by_its_environment(session: Session, environment: Environment, name: str) -> bool:
    """Say whether an environment itself uses the secret called `name`.

    That is storage (`storage_secret_ref`), a webhook that has delivered, or a webhook named by
    any stored project version of the environment, because a rollback brings an old one back.
    The manifest is read as migration 0034 reads it: ``jobs.<job>.notify.webhook``.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment.
    name
        The secret name.

    Returns
    -------
    bool
        Whether storage, a delivery or a deployed webhook names it.
    """
    if environment.storage_secret_ref == name:
        return True
    delivered = session.execute(
        select(NotificationDelivery.id)
        .where(
            NotificationDelivery.environment_id == environment.id,
            NotificationDelivery.secret_name == name,
        )
        .limit(1)
    ).first()
    if delivered is not None:
        return True
    manifests = session.execute(
        select(JobVersion.manifest_json).where(JobVersion.environment_id == environment.id)
    ).scalars()
    for manifest in manifests:
        jobs = manifest.get("jobs") if isinstance(manifest, dict) else None
        for job in jobs.values() if isinstance(jobs, dict) else ():
            notify = job.get("notify") if isinstance(job, dict) else None
            if isinstance(notify, dict) and notify.get("webhook") == name:
                return True
    return False


def _named_by_another(session: Session, name: str, use: SecretUse) -> bool:
    """Say whether an index or repository other than `use` names `name` as its credential.

    Parameters
    ----------
    session
        Database session.
    name
        The secret name.
    use
        The entry that now names it; it does not count against itself.

    Returns
    -------
    bool
        Whether another entry, of either kind, names it.
    """
    for model, kind in (
        (PackageIndex, SecretUseKind.PACKAGE_INDEX),
        (Repository, SecretUseKind.REPOSITORY),
    ):
        query = select(model.id).where(model.credential_secret == name)
        if use.kind is kind and use.target_id is not None:
            query = query.where(model.id != use.target_id)
        if session.execute(query.limit(1)).first() is not None:
            return True
    return False


def bind_named_secrets(
    session: Session,
    authz: AuthorizationService,
    p: PrincipalRef,
    *,
    name: str,
    use: SecretUse,
    registered: bool,
) -> None:
    """Bind every active secret called `name` to `use`: bind on first use.

    An index or a repository names its credential by secret name, resolved in each
    environment. Naming it binds a secret that is unbound and that nothing else wants: no other
    index or repository names it, and its environment does not use it. Anything else (bound to
    another use, named by several entries, or used by the environment) changes or settles its
    use only for a principal who may manage that environment's secrets; for one the
    environment itself uses, not even they are told to rebind it. The rows are locked while
    this runs, so two principals naming one unbound secret at once cannot both bind it. Nothing
    is committed here; the caller commits, or rolls back and calls `record_refusal`.

    Parameters
    ----------
    session
        Database session.
    authz
        Answers whether `p` may manage secrets in an environment.
    p
        The acting principal.
    name
        The secret name the index or repository names.
    use
        The index or repository, with its id.
    registered
        Whether `use` exists in the database; on a create it does not, and the refusal then
        does not send anyone to bind an entry that was rolled back.

    Raises
    ------
    SecretBindingRefused
        If binding needs `MANAGE_SECRET` in a secret's environment and `p` lacks it; the message
        names the cause and the next step.
    """
    rows = session.execute(
        select(Secret, Environment)
        .join(Environment, Environment.id == Secret.environment_id)
        .where(Secret.name == name, Secret.revoked_at.is_(None))
        .order_by(Environment.name)
        .with_for_update(of=Secret)
    ).all()
    for secret, environment in rows:
        if _bound_to(secret, use):
            continue
        scope = Securable.environment_scope(environment.name)
        env_name, secret_name = _text(environment.name), _text(name)
        who = f"Secret '{secret_name}' of environment '{env_name}'"
        current = secret.use_kind
        if current is None and used_by_its_environment(session, environment, name):
            current = SecretUseKind.ENVIRONMENT
        if current is SecretUseKind.ENVIRONMENT:
            if authz.permits(p, Action.MANAGE_SECRET, scope):
                record_binding(session, p, secret, scope, use)
                continue
            raise SecretBindingRefused(
                f"{who} serves its environment (storage or webhooks), so it cannot be the "
                f"credential of {use.wanted()}. Create a new secret for it and name that one.",
                scope,
                "secret_used_by_environment",
            )
        if current is None and not _named_by_another(session, name, use):
            record_binding(session, p, secret, scope, use)
            continue
        if authz.permits(p, Action.MANAGE_SECRET, scope):
            record_binding(session, p, secret, scope, use)
            continue
        if current is None:
            cause = (
                f"{who} is named by more than one package index or repository and is not bound "
                f"yet. Naming it for {use.wanted()} would bind it"
            )
            reason = "secret_named_by_several"
        else:
            cause = (
                f"{who} is bound to {describe_use(session, current, secret.use_target_id)}. "
                f"Naming it for {use.wanted()} would change its use"
            )
            reason = "secret_bound_elsewhere"
        step = (
            f"Ask someone who may manage secrets in '{env_name}' to run: "
            f"{bind_command(environment.name, name, use)}"
            if registered
            else f"Register {use.wanted()} without a credential first, then ask someone who may "
            f"manage secrets in '{env_name}' to run: {bind_command(environment.name, name, use)}"
        )
        raise SecretBindingRefused(
            f"{cause}, which needs the right to manage secrets in '{env_name}' (the ADMIN role). "
            f"{step}",
            scope,
            reason,
        )


def require_secret_management_for_url_change(
    session: Session, authz: AuthorizationService, p: PrincipalRef, use: SecretUse
) -> None:
    """Refuse to move an index or repository to another URL without the right.

    The URL is where a bound secret goes: changing it changes the secret's use as surely as
    binding it elsewhere would. The check does not look at the credential the entry names
    now, because clearing that name leaves the secret bound and would free the URL. The bound
    secrets are locked, so a bind that runs at the same time waits and then sees the outcome.

    Parameters
    ----------
    session
        Database session.
    authz
        Answers whether `p` may manage secrets in an environment.
    p
        The acting principal.
    use
        The index or repository whose URL changes.

    Raises
    ------
    SecretBindingRefused
        If a secret bound to it lives in an environment where `p` may not manage secrets.
    """
    rows = session.execute(
        select(Secret.name, Environment.name)
        .join(Environment, Environment.id == Secret.environment_id)
        .where(
            Secret.use_kind == use.kind,
            Secret.use_target_id == use.target_id,
            Secret.revoked_at.is_(None),
        )
        .order_by(Environment.name)
        .with_for_update(of=Secret)
    ).all()
    for secret_name, environment in rows:
        scope = Securable.environment_scope(environment)
        if not authz.permits(p, Action.MANAGE_SECRET, scope):
            raise SecretBindingRefused(
                f"Changing the URL of {use.wanted()} would send secret '{_text(secret_name)}' of "
                f"environment '{_text(environment)}' to another host. That needs the right to "
                f"manage secrets in '{_text(environment)}' (the ADMIN role). Ask someone who may "
                f"manage secrets in '{_text(environment)}' to change it, or bind the secret "
                f"elsewhere first: lakana secret bind {shlex.quote(_text(secret_name))} --env "
                f"{shlex.quote(_text(environment))} (with --index NAME, --repository NAME or "
                "--own-env)",
                scope,
                "url_moves_bound_secret",
            )
