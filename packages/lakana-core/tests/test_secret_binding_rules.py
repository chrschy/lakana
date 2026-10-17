"""Binding a secret: who may, and what naming a secret from an index or repository does."""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    PackageIndex,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Secret,
    SecretUseKind,
)
from lakana_core.projects.package_indexes import PackageIndexService
from lakana_core.projects.repositories import RepositoryService
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.secrets.uses import SecretUse, resolve_use

_KEYS = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())


def _principal(session: Session, name: str) -> PrincipalRef:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    PrincipalRef
        A reference to it.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
    session.commit()
    return ref


@pytest.fixture
def world(session: Session) -> dict[str, PrincipalRef]:
    """Two environments, an administrator and a holder of the two manage actions alone.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    dict
        ``admin`` and ``manager``.
    """
    for name in ("dev", "prod"):
        session.add(Environment(id=uuid.uuid4(), name=name, storage_root=f"file:///srv/{name}"))
    session.commit()
    admin, manager = _principal(session, "admin"), _principal(session, "manager")
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    for action in (Action.MANAGE_PACKAGE_INDEX, Action.MANAGE_REPOSITORY):
        grants.grant(admin, principal="manager", action=action, kind=SecurableKind.INSTANCE)
    return {"admin": admin, "manager": manager}


def _secret(session: Session, environment: str, name: str) -> Secret:
    """Create a secret as an administrator would, unbound unless something names it.

    Parameters
    ----------
    session
        Database session.
    environment
        Its environment.
    name
        Its name.

    Returns
    -------
    Secret
        The row.
    """
    admin = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    ref = PrincipalRef(id=admin.id, name=admin.name, type=admin.type)
    return SecretService(session, AllowAllAuthorization(), _KEYS).create(
        ref, environment=environment, name=name, values={"password": "pw"}
    )


def _uses(session: Session, name: str) -> dict[str, tuple[SecretUseKind | None, uuid.UUID | None]]:
    """Read the use of every secret called `name`, by environment.

    Parameters
    ----------
    session
        Database session.
    name
        The secret name.

    Returns
    -------
    dict
        Environment name → (kind, target id).
    """
    session.expire_all()
    rows = session.execute(
        select(Environment.name, Secret.use_kind, Secret.use_target_id)
        .join(Environment, Environment.id == Secret.environment_id)
        .where(Secret.name == name)
    ).all()
    return {env: (kind, target) for env, kind, target in rows}


def _url(session: Session, model: type[PackageIndex] | type[Repository], row_id: uuid.UUID) -> str:
    """Read the stored URL of an index or repository afresh.

    Parameters
    ----------
    session
        Database session.
    model
        `PackageIndex` or `Repository`.
    row_id
        Its id.

    Returns
    -------
    str
        The URL as committed.
    """
    session.expire_all()
    row = session.get(model, row_id)
    assert row is not None
    return row.url


def _indexes(session: Session) -> PackageIndexService:
    """Build the index service on real grants.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PackageIndexService
        The service.
    """
    return PackageIndexService(session, GrantAuthorization(session))


def test_naming_an_unbound_secret_binds_it_in_every_environment(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Bind on first use: the manager may name an unbound secret, and that binds it."""
    _secret(session, "dev", "pypi")
    _secret(session, "prod", "pypi")

    row = _indexes(session).create(
        world["manager"],
        name="corp",
        url="https://corp.example.org/simple",
        credential_secret="pypi",
    )

    assert _uses(session, "pypi") == {
        "dev": (SecretUseKind.PACKAGE_INDEX, row.id),
        "prod": (SecretUseKind.PACKAGE_INDEX, row.id),
    }
    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.SECRET_BOUND)
        )
        .scalars()
        .all()
    )
    assert sorted(e.detail["target"] for e in events) == ["corp", "corp"]


def test_a_secret_bound_elsewhere_needs_manage_secret_to_be_named(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The manager alone is refused and nothing is written; an administrator rebinds."""
    _secret(session, "prod", "pypi")
    first = _indexes(session).create(
        world["manager"], name="a", url="https://a.example.org/simple", credential_secret="pypi"
    )

    with pytest.raises(PermissionDenied) as caught:
        _indexes(session).create(
            world["manager"], name="b", url="https://b.example.org/simple", credential_secret="pypi"
        )

    assert str(caught.value) == (
        "Secret 'pypi' of environment 'prod' is bound to package index 'a'. Naming it for "
        "package index 'b' would change its use, which needs the right to manage secrets in "
        "'prod' (the ADMIN role): lakana secret bind pypi --env prod --index b"
    )
    assert session.execute(select(PackageIndex.name)).scalars().all() == ["a"]
    assert _uses(session, "pypi") == {"prod": (SecretUseKind.PACKAGE_INDEX, first.id)}
    second = _indexes(session).create(
        world["admin"], name="b", url="https://b.example.org/simple", credential_secret="pypi"
    )
    assert _uses(session, "pypi") == {"prod": (SecretUseKind.PACKAGE_INDEX, second.id)}


def test_a_storage_secret_counts_as_bound_to_its_environment(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """An unbound secret that an environment's storage names is not free for an index."""
    _secret(session, "prod", "s3")
    env = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    env.storage_secret_ref = "s3"
    session.commit()

    with pytest.raises(PermissionDenied, match="is bound to its environment"):
        _indexes(session).create(
            world["manager"],
            name="corp",
            url="https://corp.example.org/simple",
            credential_secret="s3",
        )
    assert _uses(session, "s3") == {"prod": (None, None)}


def test_a_re_registered_index_of_the_same_name_cannot_take_the_secret(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Deleting `corp` and registering `corp` again elsewhere does not carry the binding."""
    _secret(session, "prod", "pypi")
    service = _indexes(session)
    service.create(
        world["manager"],
        name="corp",
        url="https://corp.example.org/simple",
        credential_secret="pypi",
    )
    service.delete(world["manager"], "corp")

    with pytest.raises(PermissionDenied, match="bound to a package index that was removed"):
        service.create(
            world["manager"],
            name="corp",
            url="https://evil.example.org/simple",
            credential_secret="pypi",
        )


_URL_REFUSAL = (
    "Changing the URL of {use} would send secret '{secret}' of environment 'prod' to another "
    "host. That needs the right to manage secrets in 'prod' (the ADMIN role). Ask someone who "
    "may manage secrets in 'prod' to change it, or bind the secret elsewhere first: "
    "lakana secret bind {secret} --env prod (with --index NAME, --repository NAME or --own-env)"
)


def test_changing_the_url_of_a_credentialed_index_needs_manage_secret(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The URL is part of the use: moving it moves the secret to another host."""
    _secret(session, "prod", "pypi")
    service = _indexes(session)
    service.create(
        world["manager"],
        name="corp",
        url="https://corp.example.org/simple",
        credential_secret="pypi",
    )

    with pytest.raises(PermissionDenied) as caught:
        service.update(world["manager"], "corp", {"url": "https://evil.example.org/simple"})

    assert str(caught.value) == _URL_REFUSAL.format(use="package index 'corp'", secret="pypi")

    def stored() -> str:
        session.expire_all()
        return session.execute(select(PackageIndex.url)).scalar_one()

    assert stored() == "https://corp.example.org/simple"
    service.update(world["manager"], "corp", {"credential_secret": None})
    with pytest.raises(PermissionDenied):
        service.update(world["manager"], "corp", {"url": "https://other.example.org/simple"})
    assert stored() == "https://corp.example.org/simple"
    service.update(world["admin"], "corp", {"url": "https://moved.example.org/simple"})
    assert stored() == "https://moved.example.org/simple"


def test_clearing_the_credential_does_not_free_the_url(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Clear, move, name again: the secret stays bound to the index, so the move is refused."""
    _secret(session, "prod", "pypi")
    service = _indexes(session)
    row = service.create(
        world["manager"],
        name="corp",
        url="https://corp.example.org/simple",
        credential_secret="pypi",
    )

    service.update(world["manager"], "corp", {"credential_secret": None})
    with pytest.raises(PermissionDenied, match="Changing the URL of package index 'corp'"):
        service.update(world["manager"], "corp", {"url": "https://evil.example.org/simple"})
    with pytest.raises(PermissionDenied, match="Changing the URL of package index 'corp'"):
        service.update(
            world["manager"],
            "corp",
            {"url": "https://evil.example.org/simple", "credential_secret": "pypi"},
        )

    assert _url(session, PackageIndex, row.id) == "https://corp.example.org/simple"
    assert _uses(session, "pypi") == {"prod": (SecretUseKind.PACKAGE_INDEX, row.id)}


def test_repositories_follow_the_same_rules(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Naming binds on first use, and a URL change of a credentialed repository is guarded."""
    _secret(session, "prod", "git")
    service = RepositoryService(session, GrantAuthorization(session))
    repo = service.create(
        world["manager"],
        name="analytics",
        kind=RepositoryKind.HTTPS,
        url="https://git.example.org/analytics.git",
        credential_secret="git",
    )
    assert _uses(session, "git") == {"prod": (SecretUseKind.REPOSITORY, repo.id)}

    with pytest.raises(PermissionDenied) as caught:
        service.update(world["manager"], "analytics", {"url": "https://evil.example.org/a.git"})
    assert str(caught.value) == _URL_REFUSAL.format(use="repository 'analytics'", secret="git")
    assert session.execute(select(Repository.url)).scalar_one() == repo.url
    with pytest.raises(PermissionDenied, match="is bound to repository 'analytics'"):
        service.create(
            world["manager"],
            name="other",
            kind=RepositoryKind.HTTPS,
            url="https://git.example.org/other.git",
            credential_secret="git",
        )


def test_create_binds_on_first_use_or_to_the_use_given(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """A new secret takes the one entry that names it; two entries leave it unbound."""
    index = _indexes(session).create(
        world["admin"], name="corp", url="https://corp.example.org/simple", credential_secret="pypi"
    )
    _indexes(session).create(
        world["admin"], name="twin", url="https://twin.example.org/simple", credential_secret="both"
    )
    _indexes(session).create(
        world["admin"],
        name="twin2",
        url="https://twin2.example.org/simple",
        credential_secret="both",
    )
    secrets = SecretService(session, AllowAllAuthorization(), _KEYS)

    created = secrets.create(
        world["admin"], environment="prod", name="pypi", values={"password": "p"}
    )
    shared = secrets.create(
        world["admin"], environment="prod", name="both", values={"password": "p"}
    )
    storage = secrets.create(
        world["admin"],
        environment="prod",
        name="s3",
        values={"k": "v"},
        use=SecretUse.environment(),
    )

    assert (created.use_kind, created.use_target_id) == (SecretUseKind.PACKAGE_INDEX, index.id)
    assert (shared.use_kind, shared.use_target_id) == (None, None)
    assert (storage.use_kind, storage.use_target_id) == (SecretUseKind.ENVIRONMENT, None)


def test_bind_needs_manage_secret_checks_the_target_and_audits(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Only a secret manager binds; an unknown target or a revoked secret is refused."""
    secret = _secret(session, "prod", "both")
    index = _indexes(session).create(
        world["admin"], name="corp", url="https://corp.example.org/simple"
    )
    granted = SecretService(session, GrantAuthorization(session), _KEYS)

    with pytest.raises(PermissionDenied):
        granted.bind(world["manager"], secret.id, SecretUse.package_index(index))
    bound = granted.bind(
        world["admin"], secret.id, resolve_use(session, SecretUseKind.PACKAGE_INDEX, "corp")
    )

    assert (bound.use_kind, bound.use_target_id) == (SecretUseKind.PACKAGE_INDEX, index.id)
    event = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.SECRET_BOUND)
        )
        .scalars()
        .one()
    )
    assert event.detail == {
        "name": "both",
        "use": "package_index",
        "target": "corp",
        "previous": None,
    }
    with pytest.raises(NotFoundError, match="No package index named 'nope'"):
        resolve_use(session, SecretUseKind.PACKAGE_INDEX, "nope")
    with pytest.raises(ValidationError, match="takes no name"):
        resolve_use(session, SecretUseKind.ENVIRONMENT, "corp")
    granted.revoke(world["admin"], secret.id)
    with pytest.raises(ValidationError, match="has been revoked"):
        granted.bind(world["admin"], secret.id, SecretUse.environment())


def test_clearing_the_credential_does_not_free_the_url_of_a_repository(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The same bypass is closed for repositories, and an administrator may still move it."""
    _secret(session, "prod", "git")
    service = RepositoryService(session, GrantAuthorization(session))
    repo = service.create(
        world["manager"],
        name="analytics",
        kind=RepositoryKind.HTTPS,
        url="https://git.example.org/analytics.git",
        credential_secret="git",
    )

    service.update(world["manager"], "analytics", {"credential_secret": None})
    with pytest.raises(PermissionDenied, match="Changing the URL of repository 'analytics'"):
        service.update(world["manager"], "analytics", {"url": "https://evil.example.org/a.git"})
    assert _url(session, Repository, repo.id) == "https://git.example.org/analytics.git"

    service.update(world["admin"], "analytics", {"url": "https://git.example.org/moved.git"})
    assert _url(session, Repository, repo.id) == "https://git.example.org/moved.git"
