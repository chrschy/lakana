"""What a secret may be used for, and the refusal when it is asked for anything else.

A secret has a use: a package index or a repository (by id), or its environment (storage,
webhooks and, later, tasks). An unbound secret serves its environment only. The four places
that decrypt a secret (`StorageResolver`, `lakana_worker.deploy`,
`lakana_worker.index_credentials`, `lakana_worker.notify`) call `require_use` first, so a
holder of `MANAGE_PACKAGE_INDEX` or `MANAGE_REPOSITORY` who names another secret cannot send
it to a host of their choice.
"""

from __future__ import annotations

import dataclasses
import shlex
import uuid
from dataclasses import dataclass, field
from typing import TypeVar

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
_ENVIRONMENT_USE = "its environment (storage and webhooks)"
_GROUPS = {SecretUseKind.PACKAGE_INDEX: "index", SecretUseKind.REPOSITORY: "repo"}


def plain(name: str) -> str:
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
    target_url
        The URL the secret goes to, as the request leaves the entry (the new URL of an update,
        the URL of an entry being created); recorded when the secret is bound. It plays no part
        in equality.
    """

    kind: SecretUseKind
    target_id: uuid.UUID | None = None
    target_name: str | None = None
    target_url: str | None = field(default=None, compare=False)

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
    def package_index(cls, row: PackageIndex, url: str | None = None) -> SecretUse:
        """Name the use "credential of this package index".

        Parameters
        ----------
        row
            The index.
        url
            The URL the index will have when the request ends; the row's own when omitted.

        Returns
        -------
        SecretUse
            The use.
        """
        return cls(SecretUseKind.PACKAGE_INDEX, row.id, row.name, url or row.url)

    @classmethod
    def repository(cls, row: Repository, url: str | None = None) -> SecretUse:
        """Name the use "credential of this repository".

        Parameters
        ----------
        row
            The repository.
        url
            The URL it will have when the request ends; the row's own when omitted.

        Returns
        -------
        SecretUse
            The use.
        """
        return cls(SecretUseKind.REPOSITORY, row.id, row.name, url or row.url)

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
        return f"{_OPTIONS[self.kind]} {shlex.quote(plain(self.target_name or ''))}"

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
        f"lakana secret bind {shlex.quote(plain(name))} --env {shlex.quote(plain(environment))} "
        f"{use.option()}"
    )


def own_secret_steps(environment: str, use: SecretUse) -> str:
    """Write the two commands that give an index or repository a secret of its own.

    Parameters
    ----------
    environment
        The environment the secret lives in.
    use
        The index or repository, with its name.

    Returns
    -------
    str
        ``lakana secret create NEW ... --values-stdin``, then the ``update`` that names it, each
        in backticks, quoted for a shell.
    """
    name = shlex.quote(plain(use.target_name or ""))
    return (
        f"`lakana secret create NEW --env {shlex.quote(plain(environment))} {use.option()} "
        f"--values-stdin`, then `lakana {_GROUPS[use.kind]} update {name} --credential-secret NEW`"
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
    if use.kind is SecretUseKind.ENVIRONMENT:
        # Binding it to the environment would take it from the index or repository that
        # needs it, so the first suggestion is a secret of its own.
        raise SecretUseRefused(
            f"{who} is bound to {current}, not to {use.wanted()}, so Lakana does not use it "
            f"there. Name a secret of its own instead; or, if this secret really belongs to the "
            f"environment, an administrator binds it, and {current} then loses it: {command}"
        )
    raise SecretUseRefused(
        f"{who} is bound to {current}, not to {use.wanted()}, so Lakana does not use it there. "
        f"Give {use.wanted()} a secret of its own: {own_secret_steps(environment_name, use)}. "
        f"Moving this secret instead breaks {current}: {command}"
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
            "url": use.target_url,
            "previous": secret.use_kind.value if secret.use_kind is not None else None,
        },
    )
    secret.use_kind, secret.use_target_id = use.kind, use.target_id


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
            f"No {noun} named '{plain(target)}'; `{listing}` shows the registered ones."
        )
    return SecretUse(kind, row.id, row.name, row.url)


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


_SHORT = {SecretUseKind.PACKAGE_INDEX: "index", SecretUseKind.REPOSITORY: "repository"}


_Row = TypeVar("_Row", PackageIndex, Repository)


def lock_row(session: Session, model: type[_Row], row_id: uuid.UUID) -> _Row | None:
    """Lock an index's or repository's row and read it afresh, until the transaction ends.

    Parameters
    ----------
    session
        Database session.
    model
        `PackageIndex` or `Repository`.
    row_id
        The row's id.

    Returns
    -------
    PackageIndex | Repository | None
        The row as committed now, or `None` when it was removed.
    """
    return session.execute(
        select(model)
        .where(model.id == row_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def lock_entry(session: Session, use: SecretUse) -> SecretUse:
    """Lock the index or repository row a binding points at, until the transaction ends.

    Everything that binds a secret to an entry, names a secret for it or moves its URL takes
    this lock first (and the secret rows after it), so those steps run one after the other
    and the second one sees what the first committed.

    Parameters
    ----------
    session
        Database session.
    use
        The index or repository; the environment has no row and nothing is locked.

    Returns
    -------
    SecretUse
        `use` with the URL the row has under the lock, which is the URL the secret goes to;
        `use` itself for the environment.

    Raises
    ------
    NotFoundError
        If the row has been removed.
    """
    if use.target_id is None:
        return use
    if use.kind is SecretUseKind.PACKAGE_INDEX:
        found = lock_row(session, PackageIndex, use.target_id)
    else:
        found = lock_row(session, Repository, use.target_id)
    if found is None:
        raise NotFoundError(f"The {_NOUNS[use.kind]} was removed by someone else; look again.")
    return dataclasses.replace(use, target_url=found.url)


def bind_or_require_named_secrets(
    session: Session,
    authz: AuthorizationService,
    p: PrincipalRef,
    *,
    name: str,
    use: SecretUse,
    registered: bool,
) -> None:
    """Make every active secret called `name` serve this index or repository, or refuse.

    A secret already bound to `use` passes. An unbound secret binds to `use` (audited, with
    the actor) when `p` may manage secrets in its environment, and the environment does not use
    it; for anyone else naming it is refused. A secret bound to another use, or one the
    environment itself uses (storage, a webhook), is refused to everyone with the explicit way
    to move it (`lakana secret bind`). The secret rows are locked while this runs and read
    afresh, so a bind that commits at the same moment is seen. Nothing is committed here.

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
        sends the caller to register it without a credential first.

    Raises
    ------
    SecretBindingRefused
        If a secret of that name cannot serve `use`; the message names the cause and the
        next step, and the exception carries the environment scope and the reason.
    """
    rows = session.execute(
        select(Secret, Environment)
        .join(Environment, Environment.id == Secret.environment_id)
        .where(Secret.name == name, Secret.revoked_at.is_(None))
        .order_by(Environment.name)
        .with_for_update(of=Secret)
        .execution_options(populate_existing=True)
    ).all()
    short = _SHORT[use.kind]
    for secret, environment in rows:
        if _bound_to(secret, use):
            continue
        env_name = plain(environment.name)
        who = f"Secret '{plain(name)}' of environment '{env_name}'"
        scope = Securable.environment_scope(environment.name)
        first = (
            "Ask" if registered else f"Register the {short} without a credential first, then ask"
        )
        command = bind_command(environment.name, name, use)
        steps = own_secret_steps(environment.name, use)
        current = secret.use_kind
        if current is SecretUseKind.ENVIRONMENT or (
            current is None and used_by_its_environment(session, environment, name)
        ):
            # Storage stops working without its secret, so only a webhook's secret is offered
            # as a deliberate move.
            move = (
                ""
                if environment.storage_secret_ref == name
                else f", or to move this one on purpose (the environment then loses it): {command}"
            )
            raise SecretBindingRefused(
                f"{who} serves its environment (storage or webhooks), so naming it for "
                f"{use.wanted()} will not move it. {first} someone who may manage secrets in "
                f"'{env_name}' to give the {short} a secret of its own: {steps}{move}",
                scope,
                "secret_used_by_environment",
            )
        if current is None and authz.permits(p, Action.MANAGE_SECRET, scope):
            record_binding(session, p, secret, scope, use)
            continue
        if current is None:
            cause, reason = f"is not bound to {use.wanted()}", "secret_not_bound_to_entry"
        else:
            described = describe_use(session, current, secret.use_target_id)
            cause, reason = (
                f"is bound to {described}, not to {use.wanted()}",
                "secret_bound_elsewhere",
            )
        this = "this" if registered else "the"
        if current is None:
            advice = (
                f"{first} someone who may manage secrets in '{env_name}' to bind it to "
                f"{this} {short}: {command}"
            )
        else:
            advice = (
                f"{first} someone who may manage secrets in '{env_name}' to give {this} {short} "
                f"a secret of its own: {steps}. Moving this secret instead breaks {described}: "
                f"{command}"
            )
        raise SecretBindingRefused(f"{who} {cause}. {advice}", scope, reason)


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
        .execution_options(populate_existing=True)
    ).all()
    for secret_name, environment in rows:
        scope = Securable.environment_scope(environment)
        if not authz.permits(p, Action.MANAGE_SECRET, scope):
            raise SecretBindingRefused(
                f"Changing the URL of {use.wanted()} would send secret '{plain(secret_name)}' of "
                f"environment '{plain(environment)}' to another host. That needs the right to "
                f"manage secrets in '{plain(environment)}' (the ADMIN role). Ask someone who may "
                f"manage secrets in '{plain(environment)}' to change the URL, or to bind the "
                f"secret elsewhere first: lakana secret bind {shlex.quote(plain(secret_name))} "
                f"--env {shlex.quote(plain(environment))} (with --index NAME, --repository NAME or "
                "--own-env)",
                scope,
                "url_moves_bound_secret",
            )
