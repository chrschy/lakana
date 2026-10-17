"""Binding a secret: who may, and what naming a secret from an index or repository does."""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    JobVersion,
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
    assert {e.principal_id for e in events} == {world["manager"].id}
    assert [e.detail["previous"] for e in events] == [None, None]


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
        "'prod' (the ADMIN role). Register package index 'b' without a credential first, then "
        "ask someone who may manage secrets in 'prod' to run: "
        "lakana secret bind pypi --env prod --index b"
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

    with pytest.raises(PermissionDenied) as caught:
        _indexes(session).create(
            world["manager"],
            name="corp",
            url="https://corp.example.org/simple",
            credential_secret="s3",
        )
    assert str(caught.value) == (
        "Secret 's3' of environment 'prod' serves its environment (storage or webhooks), so it "
        "cannot be the credential of package index 'corp'. Create a new secret for it and name "
        "that one."
    )
    assert "lakana secret bind" not in str(caught.value)
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


def _auditing(session: Session) -> AuditingAuthorization:
    """Build real grants that record denials, as the server does.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    AuditingAuthorization
        The authorization service.
    """
    return AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(10_000))


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


def _bound_events(session: Session) -> int:
    """Count the `SECRET_BOUND` events.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    int
        The number of rows.
    """
    return len(
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.SECRET_BOUND)
        )
        .scalars()
        .all()
    )


def _ambiguous(session: Session, world: dict[str, PrincipalRef]) -> None:
    """Name `pypi` from index `a` and index `b`, then create the secret: it stays unbound.

    Parameters
    ----------
    session
        Database session.
    world
        The fixture's principals.
    """
    service = _indexes(session)
    for name in ("a", "b"):
        service.create(
            world["admin"],
            name=name,
            url=f"https://{name}.example.org/simple",
            credential_secret="pypi",
        )
    _secret(session, "prod", "pypi")
    assert _uses(session, "pypi") == {"prod": (None, None)}


def test_a_manager_cannot_claim_a_secret_that_several_entries_name(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Ambiguous secrets are for an administrator, also when the URL is moved first."""
    _ambiguous(session, world)
    service = PackageIndexService(session, _auditing(session))

    service.update(world["manager"], "b", {"credential_secret": "pypi"})
    assert _uses(session, "pypi") == {"prod": (None, None)}
    service.update(world["manager"], "b", {"credential_secret": None})
    service.update(world["manager"], "b", {"url": "https://evil.example.org/simple"})
    with pytest.raises(PermissionDenied) as caught:
        service.update(world["manager"], "b", {"credential_secret": "pypi"})

    assert str(caught.value) == (
        "Secret 'pypi' of environment 'prod' is named by more than one package index or "
        "repository and is not bound yet. Naming it for package index 'b' would bind it, which "
        "needs the right to manage secrets in 'prod' (the ADMIN role). Ask someone who may "
        "manage secrets in 'prod' to run: lakana secret bind pypi --env prod --index b"
    )
    assert _uses(session, "pypi") == {"prod": (None, None)}
    assert _denials(session) == [(world["manager"].id, "secret_named_by_several")]
    assert _bound_events(session) == 0
    service.update(world["admin"], "b", {"credential_secret": "pypi"})
    assert _uses(session, "pypi")["prod"][0] is SecretUseKind.PACKAGE_INDEX


def test_a_repository_and_an_index_naming_one_secret_leave_it_to_an_administrator(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The check holds across kinds, in both directions, and on create."""
    _indexes(session).create(
        world["admin"], name="a", url="https://a.example.org/simple", credential_secret="tok"
    )
    repositories = RepositoryService(session, _auditing(session))
    repositories.create(
        world["admin"],
        name="r",
        kind=RepositoryKind.HTTPS,
        url="https://git.example.org/r.git",
        credential_secret="tok",
    )
    _secret(session, "prod", "tok")
    assert _uses(session, "tok") == {"prod": (None, None)}

    repositories.update(world["manager"], "r", {"credential_secret": None})
    with pytest.raises(PermissionDenied, match="named by more than one"):
        repositories.update(world["manager"], "r", {"credential_secret": "tok"})
    indexes = PackageIndexService(session, _auditing(session))
    indexes.update(world["manager"], "a", {"credential_secret": None})
    repositories.update(world["admin"], "r", {"credential_secret": "tok"})
    with pytest.raises(PermissionDenied, match="is bound to repository 'r'"):
        indexes.update(world["manager"], "a", {"credential_secret": "tok"})
    with pytest.raises(PermissionDenied, match="Register package index 'c' without a credential"):
        indexes.create(
            world["manager"], name="c", url="https://c.example.org/simple", credential_secret="tok"
        )
    assert [reason for _, reason in _denials(session)] == [
        "secret_named_by_several",
        "secret_bound_elsewhere",
        "secret_bound_elsewhere",
    ]


def test_a_secret_a_deployed_webhook_names_cannot_be_taken_by_a_manager(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The environment uses it: only a secret manager may bind it, and nobody is told to rebind."""
    _secret(session, "prod", "hook")
    prod = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    repository = RepositoryService(session, AllowAllAuthorization()).create(
        world["admin"], name="r", kind=RepositoryKind.HTTPS, url="https://git.example.org/r.git"
    )
    session.add(
        JobVersion(
            id=uuid.uuid4(),
            repository_id=repository.id,
            environment_id=prod.id,
            commit_sha="a" * 40,
            manifest_json={"jobs": {"nightly": {"notify": {"webhook": "hook"}}}},
            code_uri="file:///code",
            code_sha256="0" * 64,
        )
    )
    session.commit()
    service = PackageIndexService(session, _auditing(session))

    with pytest.raises(PermissionDenied) as caught:
        service.create(
            world["manager"],
            name="corp",
            url="https://corp.example.org/simple",
            credential_secret="hook",
        )

    assert "serves its environment (storage or webhooks)" in str(caught.value)
    assert "lakana secret bind" not in str(caught.value)
    assert _uses(session, "hook") == {"prod": (None, None)}
    assert _denials(session) == [(world["manager"].id, "secret_used_by_environment")]
    assert session.execute(select(PackageIndex.name)).scalars().all() == []
    service.create(
        world["admin"],
        name="corp",
        url="https://corp.example.org/simple",
        credential_secret="hook",
    )
    assert _uses(session, "hook")["prod"][0] is SecretUseKind.PACKAGE_INDEX


def test_an_unchanged_credential_binds_nothing(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """An unrelated update does not re-run the bind, so it neither fails nor binds."""
    repositories = RepositoryService(session, GrantAuthorization(session))
    repositories.create(
        world["admin"],
        name="r",
        kind=RepositoryKind.HTTPS,
        url="https://git.example.org/r.git",
        credential_secret="git",
    )
    _indexes(session).create(
        world["admin"], name="a", url="https://a.example.org/simple", credential_secret="git"
    )
    _secret(session, "prod", "git")
    session.expire_all()
    before = _uses(session, "git")

    repositories.update(world["manager"], "r", {"default_ref": "main"})
    repositories.update(world["manager"], "r", {"credential_secret": "git"})

    assert _uses(session, "git") == before
    assert before == {"prod": (None, None)}


def test_a_revoked_secret_neither_blocks_nor_is_bound(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Only active secrets count."""
    _secret(session, "dev", "pypi")
    old = _secret(session, "prod", "pypi")
    secrets = SecretService(session, AllowAllAuthorization(), _KEYS)
    index = _indexes(session).create(
        world["admin"], name="a", url="https://a.example.org/simple", credential_secret="pypi"
    )
    secrets.revoke(world["admin"], old.id)
    _secret(session, "prod", "other")

    _indexes(session).create(
        world["manager"], name="b", url="https://b.example.org/simple", credential_secret="other"
    )
    with pytest.raises(PermissionDenied):
        _indexes(session).create(
            world["manager"], name="c", url="https://c.example.org/simple", credential_secret="pypi"
        )
    assert _uses(session, "pypi")["dev"] == (SecretUseKind.PACKAGE_INDEX, index.id)


def test_a_secret_bound_to_its_environment_or_a_repository_is_refused_to_a_manager(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Explicit uses count as bound, and the environment's can be taken by an administrator."""
    secrets = SecretService(session, AllowAllAuthorization(), _KEYS)
    secrets.create(
        world["admin"],
        environment="prod",
        name="own",
        values={"k": "v"},
        use=SecretUse.environment(),
    )
    with pytest.raises(PermissionDenied, match="serves its environment"):
        _indexes(session).create(
            world["manager"], name="a", url="https://a.example.org/simple", credential_secret="own"
        )
    _indexes(session).create(
        world["admin"], name="a", url="https://a.example.org/simple", credential_secret="own"
    )
    assert _uses(session, "own")["prod"][0] is SecretUseKind.PACKAGE_INDEX


def test_a_re_registered_repository_of_the_same_name_cannot_take_the_secret(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The binding follows the id, so a repository registered again does not inherit it."""
    _secret(session, "prod", "git")
    service = RepositoryService(session, GrantAuthorization(session))
    service.create(
        world["manager"],
        name="r",
        kind=RepositoryKind.HTTPS,
        url="https://git.example.org/r.git",
        credential_secret="git",
    )
    service.delete(world["manager"], "r")

    with pytest.raises(PermissionDenied, match="bound to a repository that was removed"):
        service.create(
            world["manager"],
            name="r",
            kind=RepositoryKind.HTTPS,
            url="https://evil.example.org/r.git",
            credential_secret="git",
        )


def _scoped(session: Session, world: dict[str, PrincipalRef], *, role: bool) -> PrincipalRef:
    """Persist `devsec`: both manage actions everywhere, and secret management in dev only.

    Parameters
    ----------
    session
        Database session.
    world
        The fixture's principals.
    role
        Give the `ADMIN` role scoped to `dev` instead of the single `MANAGE_SECRET` action.

    Returns
    -------
    PrincipalRef
        The principal.
    """
    ref = _principal(session, "devsec")
    grants = GrantService(session, AllowAllAuthorization())
    for action in (Action.MANAGE_PACKAGE_INDEX, Action.MANAGE_REPOSITORY):
        grants.grant(world["admin"], principal="devsec", action=action, kind=SecurableKind.INSTANCE)
    if role:
        grants.grant(
            world["admin"],
            principal="devsec",
            role=Role.ADMIN,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
        )
    else:
        grants.grant(
            world["admin"],
            principal="devsec",
            action=Action.MANAGE_SECRET,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
        )
    return ref


@pytest.mark.parametrize("role", [False, True])
def test_rights_in_dev_never_bind_or_redirect_anything_in_prod(
    session: Session, world: dict[str, PrincipalRef], role: bool
) -> None:
    """A dev secret manager is refused for prod, and the refusal leaves dev untouched too."""
    devsec = _scoped(session, world, role=role)
    for env in ("dev", "prod"):
        _secret(session, env, "pypi")
    _secret(session, "dev", "dev-only")
    first = _indexes(session).create(
        world["admin"], name="a", url="https://a.example.org/simple", credential_secret="pypi"
    )
    other = _indexes(session).create(
        world["admin"], name="o", url="https://o.example.org/simple", credential_secret="dev-only"
    )
    service = PackageIndexService(session, _auditing(session))

    with pytest.raises(PermissionDenied, match="environment 'prod'"):
        service.create(
            devsec, name="b", url="https://b.example.org/simple", credential_secret="pypi"
        )
    with pytest.raises(PermissionDenied, match="would send secret 'pypi' of environment 'prod'"):
        service.update(devsec, "a", {"url": "https://evil.example.org/simple"})

    assert _uses(session, "pypi") == {
        "dev": (SecretUseKind.PACKAGE_INDEX, first.id),
        "prod": (SecretUseKind.PACKAGE_INDEX, first.id),
    }
    assert _url(session, PackageIndex, first.id) == "https://a.example.org/simple"
    assert [reason for _, reason in _denials(session)] == [
        "secret_bound_elsewhere",
        "url_moves_bound_secret",
    ]
    service.update(devsec, "o", {"url": "https://moved.example.org/simple"})
    assert _url(session, PackageIndex, other.id) == "https://moved.example.org/simple"


def test_a_first_use_at_secret_create_records_what_it_bound(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The audit log says which index took the new secret, and who created it."""
    index = _indexes(session).create(
        world["admin"], name="corp", url="https://corp.example.org/simple", credential_secret="pypi"
    )
    _secret(session, "prod", "pypi")

    event = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.SECRET_BOUND)
        )
        .scalars()
        .one()
    )
    assert event.detail == {
        "name": "pypi",
        "use": "package_index",
        "target": "corp",
        "previous": None,
    }
    assert _uses(session, "pypi") == {"prod": (SecretUseKind.PACKAGE_INDEX, index.id)}
