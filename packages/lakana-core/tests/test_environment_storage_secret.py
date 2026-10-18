"""Changing where an environment's storage secret goes needs MANAGE_SECRET (#134)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog import CatalogService
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    Principal,
    PrincipalType,
)
from lakana_core.storage.resolver import StorageResolver

_REFUSAL = (
    "Changing the storage options of environment 'dev' would send its storage secret 'creds' "
    "to wherever they point. That needs the right to manage secrets in 'dev' (the ADMIN role). "
    "Ask someone who may manage secrets in 'dev' to make the change: "
    "lakana env set-storage dev --option KEY=VALUE ... (--option replaces all the options, so "
    "pass every option the environment should have, for example --option s3.endpoint=URL "
    "--option s3.region=REGION)"
)


def _principal(
    session: Session, name: str, *actions: Action, secret_scope: str | None = None
) -> PrincipalRef:
    """Persist a principal holding grants of the given actions.

    `MANAGE_ENVIRONMENT` is granted at instance scope; `MANAGE_SECRET` at instance scope, or in
    the environment `secret_scope` when given.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.
    *actions
        The actions granted.
    secret_scope
        The environment to grant `MANAGE_SECRET` in instead of the instance.

    Returns
    -------
    PrincipalRef
        A reference to it.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
    session.commit()
    grants = GrantService(session, AllowAllAuthorization())
    for action in actions:
        if action is Action.MANAGE_SECRET and secret_scope is not None:
            grants.grant(
                ref,
                principal=name,
                action=action,
                kind=SecurableKind.ENVIRONMENT,
                environment=secret_scope,
            )
        else:
            grants.grant(ref, principal=name, action=action, kind=SecurableKind.INSTANCE)
    return ref


def _environment(session: Session, *, secret: str | None) -> None:
    """Add the environment `dev`, with or without a storage secret.

    Parameters
    ----------
    session
        Database session.
    secret
        The storage secret reference, or `None`.
    """
    session.add(
        Environment(
            id=uuid.uuid4(),
            name="dev",
            storage_root="s3://bucket/dev",
            storage_options={"s3.endpoint": "https://s3.example.org", "s3.region": "eu-west-1"},
            storage_secret_ref=secret,
        )
    )
    session.commit()


def _service(session: Session) -> CatalogService:
    """Build the catalog on real grants that record denials, as the server does.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    CatalogService
        The service.
    """
    authz = AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(10_000))
    return CatalogService(session, StorageResolver(session), authz)


def _denials(session: Session) -> list[tuple[uuid.UUID | None, str]]:
    """List the recorded denials as (actor, reason).

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list
        One pair per `AUTHORIZATION_DENIED` row, oldest first.
    """
    session.expire_all()
    rows = (
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
            .order_by(AuditEvent.occurred_at)
        )
        .scalars()
        .all()
    )
    return [(row.principal_id, row.detail.get("reason", "")) for row in rows]


def _options(session: Session) -> dict[str, str]:
    """Read the options of `dev` afresh.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    dict
        The stored options.
    """
    session.expire_all()
    return dict(session.execute(select(Environment.storage_options)).scalar_one())


@pytest.mark.parametrize(
    "options",
    [
        {"s3.endpoint": "https://evil.example.org", "s3.region": "eu-west-1"},
        {"s3.endpoint": "https://s3.example.org"},
        {"s3.endpoint": "https://s3.example.org", "s3.region": "eu-west-1", "s3.proxy-uri": "x"},
        {},
    ],
    ids=["endpoint", "key-removed", "key-added", "emptied"],
)
def test_options_change_needs_manage_secret_when_a_storage_secret_is_set(
    session: Session, options: dict[str, str]
) -> None:
    """MANAGE_ENVIRONMENT alone is refused and audited; with MANAGE_SECRET the change lands."""
    _environment(session, secret="creds")
    manager = _principal(session, "manager", Action.MANAGE_ENVIRONMENT)
    both = _principal(session, "both", Action.MANAGE_ENVIRONMENT, Action.MANAGE_SECRET)
    before = _options(session)

    with pytest.raises(PermissionDenied) as caught:
        _service(session).update_environment_storage(manager, "dev", options=options)

    assert str(caught.value) == _REFUSAL
    assert _options(session) == before
    assert _denials(session) == [(manager.id, "storage_options_move_secret")]
    _service(session).update_environment_storage(both, "dev", options=options)
    assert _options(session) == options


def test_an_unchanged_options_map_needs_no_extra_right(session: Session) -> None:
    """Sending the stored options again moves nothing, so it is not refused."""
    _environment(session, secret="creds")
    manager = _principal(session, "manager", Action.MANAGE_ENVIRONMENT)

    _service(session).update_environment_storage(manager, "dev", options=_options(session))

    assert _denials(session) == []


def test_without_a_storage_secret_options_need_only_manage_environment(session: Session) -> None:
    """An environment with no storage secret keeps the old rule."""
    _environment(session, secret=None)
    manager = _principal(session, "manager", Action.MANAGE_ENVIRONMENT)

    _service(session).update_environment_storage(manager, "dev", options={"s3.endpoint": "x"})

    assert _options(session) == {"s3.endpoint": "x"}
    assert _denials(session) == []


def test_naming_a_secret_stays_covered_by_manage_secret(session: Session) -> None:
    """Changing which secret the environment uses was already refused without MANAGE_SECRET."""
    _environment(session, secret="creds")
    manager = _principal(session, "manager", Action.MANAGE_ENVIRONMENT)

    with pytest.raises(PermissionDenied):
        _service(session).update_environment_storage(manager, "dev", secret_ref="other")

    session.expire_all()
    assert session.execute(select(Environment.storage_secret_ref)).scalar_one() == "creds"
    assert len(_denials(session)) == 1


def test_manage_secret_in_this_environment_is_enough(session: Session) -> None:
    """The right is environment-scoped: a grant in `dev` itself satisfies the check."""
    _environment(session, secret="creds")
    holder = _principal(
        session, "holder", Action.MANAGE_ENVIRONMENT, Action.MANAGE_SECRET, secret_scope="dev"
    )

    _service(session).update_environment_storage(holder, "dev", options={"s3.endpoint": "x"})

    assert _options(session) == {"s3.endpoint": "x"}


def test_manage_secret_in_another_environment_does_not_count(session: Session) -> None:
    """A grant in `prod` says nothing about `dev`: the change is refused and audited."""
    _environment(session, secret="creds")
    session.add(Environment(id=uuid.uuid4(), name="prod", storage_root="s3://bucket/prod"))
    session.commit()
    holder = _principal(
        session, "holder", Action.MANAGE_ENVIRONMENT, Action.MANAGE_SECRET, secret_scope="prod"
    )

    with pytest.raises(PermissionDenied):
        _service(session).update_environment_storage(holder, "dev", options={"s3.endpoint": "x"})

    assert _denials(session) == [(holder.id, "storage_options_move_secret")]


def test_early_refusals_end_the_transaction_and_release_the_row_lock(session: Session) -> None:
    """A protected or missing environment raises with nothing left open."""
    _environment(session, secret=None)
    session.execute(select(Environment)).scalar_one().is_protected = True
    session.commit()
    admin = _principal(session, "admin", Action.MANAGE_ENVIRONMENT)

    with pytest.raises(ValidationError):
        _service(session).update_environment_storage(admin, "dev", options={"a": "b"})
    assert not session.in_transaction()
    with pytest.raises(NotFoundError):
        _service(session).update_environment_storage(admin, "nope", options={"a": "b"})
    assert not session.in_transaction()


def test_attaching_a_secret_records_the_endpoint_it_approves(session: Session) -> None:
    """The audit event holds the storage endpoint (no user information), never other options."""
    _environment(session, secret=None)
    row = session.execute(select(Environment)).scalar_one()
    row.storage_options = {"s3.endpoint": "https://key:pw@s3.example.org:9000", "s3.x": "private"}
    session.commit()
    both = _principal(session, "both", Action.MANAGE_ENVIRONMENT, Action.MANAGE_SECRET)

    _service(session).update_environment_storage(both, "dev", secret_ref="creds")

    event = session.execute(
        select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.ENVIRONMENT_STORAGE_UPDATED
        )
    ).scalar_one()
    assert event.detail["storage_endpoint"] == "https://s3.example.org:9000"
    assert "private" not in str(event.detail) and "pw" not in str(event.detail)
