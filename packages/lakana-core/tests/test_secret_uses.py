"""`lakana_core.secrets.uses`: which use a secret may serve, and what a refusal says."""

from __future__ import annotations

import shlex
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.errors import NotFoundError
from lakana_core.models import (
    Connection,
    Environment,
    PackageIndex,
    Repository,
    RepositoryKind,
    Secret,
    SecretUseKind,
)
from lakana_core.secrets.uses import (
    SecretUse,
    SecretUseRefused,
    adopt_connection_secret,
    approved_storage_options,
    bind_command,
    describe_use,
    detach_from_connection,
    lock_entry,
    require_connection_takes,
    require_use,
    resolve_use,
    target_row,
)


def _environment(session: Session, name: str = "prod") -> Environment:
    """Insert an environment.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    Environment
        The row.
    """
    row = Environment(id=uuid.uuid4(), name=name, storage_root=f"file:///srv/{name}")
    session.add(row)
    session.commit()
    return row


def _secret(
    session: Session,
    environment: Environment,
    principal: PrincipalRef,
    name: str,
    kind: SecretUseKind | None = None,
    target: uuid.UUID | None = None,
) -> Secret:
    """Insert a secret row; its encrypted fields are placeholders nobody decrypts here.

    Parameters
    ----------
    session
        Database session.
    environment
        Its environment.
    principal
        Its creator.
    name
        Its name.
    kind
        Its use, or `None` for unbound.
    target
        The index or repository it is bound to.

    Returns
    -------
    Secret
        The row.
    """
    row = Secret(
        id=uuid.uuid4(),
        environment_id=environment.id,
        name=name,
        ciphertext=b"x",
        nonce=b"n" * 12,
        wrapped_data_key=b"k",
        format_version=2,
        key_id="env",
        created_by=principal.id,
        created_at=datetime.now(UTC),
        use_kind=kind,
        use_target_id=target,
    )
    session.add(row)
    session.commit()
    return row


def _index(session: Session, name: str = "corp") -> PackageIndex:
    """Register a package index row.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    PackageIndex
        The row.
    """
    row = PackageIndex(
        id=uuid.uuid4(), name=name, url=f"https://{name}.example.org/simple", credential_secret=None
    )
    session.add(row)
    session.commit()
    return row


def _repository(session: Session, name: str = "analytics") -> Repository:
    """Register a repository row.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    Repository
        The row.
    """
    row = Repository(
        id=uuid.uuid4(), name=name, kind=RepositoryKind.HTTPS, url="https://git.example.org/a.git"
    )
    session.add(row)
    session.commit()
    return row


def test_an_index_use_passes_only_for_its_own_index(
    session: Session, principal: PrincipalRef
) -> None:
    """A secret bound to `corp` serves `corp` and nothing else."""
    env = _environment(session)
    corp, other = _index(session, "corp"), _index(session, "other")
    secret = _secret(session, env, principal, "corp-token", SecretUseKind.PACKAGE_INDEX, corp.id)

    require_use(session, secret, SecretUse.package_index(corp), environment_name="prod")
    with pytest.raises(SecretUseRefused) as caught:
        require_use(session, secret, SecretUse.package_index(other), environment_name="prod")

    assert str(caught.value) == (
        "Secret 'corp-token' of environment 'prod' is bound to package index 'corp', not to "
        "package index 'other', so Lakana does not use it there. Give package index 'other' a "
        "secret of its own: `lakana secret create NEW --env prod --index other --values-stdin`, "
        "then `lakana index update other --credential-secret NEW`. Moving this secret instead "
        "breaks package index 'corp': lakana secret bind corp-token --env prod --index other"
    )


def test_an_unbound_secret_is_refused_for_an_index_with_the_command(
    session: Session, principal: PrincipalRef
) -> None:
    """An unbound secret reaches no index host until an administrator binds it."""
    env = _environment(session)
    corp = _index(session)
    secret = _secret(session, env, principal, "corp-token")

    with pytest.raises(SecretUseRefused) as caught:
        require_use(session, secret, SecretUse.package_index(corp), environment_name="prod")

    assert str(caught.value) == (
        "Secret 'corp-token' of environment 'prod' is not bound to a use yet, so Lakana does not "
        "send it to package index 'corp'. An administrator binds it: "
        "lakana secret bind corp-token --env prod --index corp"
    )


def test_a_repository_secret_is_refused_as_an_index_credential(
    session: Session, principal: PrincipalRef
) -> None:
    """The kind counts as well as the target."""
    env = _environment(session)
    corp, repo = _index(session), _repository(session)
    secret = _secret(session, env, principal, "git-token", SecretUseKind.REPOSITORY, repo.id)

    require_use(session, secret, SecretUse.repository(repo), environment_name="prod")
    with pytest.raises(SecretUseRefused, match="is bound to repository 'analytics'"):
        require_use(session, secret, SecretUse.package_index(corp), environment_name="prod")


@pytest.mark.parametrize(
    ("kind", "allowed"),
    [
        (None, True),
        (SecretUseKind.ENVIRONMENT, True),
        (SecretUseKind.PACKAGE_INDEX, False),
        (SecretUseKind.REPOSITORY, False),
    ],
)
def test_the_environment_use_takes_unbound_and_environment_secrets_only(
    session: Session, principal: PrincipalRef, kind: SecretUseKind | None, allowed: bool
) -> None:
    """Storage and webhooks keep working with unbound secrets; a bound credential stays put.

    Parameters
    ----------
    session
        Database session.
    principal
        The creator.
    kind
        The secret's use.
    allowed
        Whether the environment's own use may read it.
    """
    env = _environment(session)
    target = _index(session).id if kind is SecretUseKind.PACKAGE_INDEX else None
    if kind is SecretUseKind.REPOSITORY:
        target = _repository(session).id
    secret = _secret(session, env, principal, "s3", kind, target)

    if allowed:
        require_use(session, secret, SecretUse.environment(), environment_name="prod")
    else:
        with pytest.raises(SecretUseRefused, match="not to its environment"):
            require_use(session, secret, SecretUse.environment(), environment_name="prod")


def test_a_removed_target_is_named_as_removed(session: Session, principal: PrincipalRef) -> None:
    """A binding to a deleted index never becomes unbound by itself."""
    env = _environment(session)
    gone = _index(session, "gone")
    secret = _secret(session, env, principal, "t", SecretUseKind.PACKAGE_INDEX, gone.id)
    session.delete(gone)
    session.commit()

    assert describe_use(session, secret.use_kind, secret.use_target_id) == (
        "a package index that was removed"
    )
    corp = _index(session, "gone")
    with pytest.raises(SecretUseRefused, match="bound to a package index that was removed"):
        require_use(session, secret, SecretUse.package_index(corp), environment_name="prod")


def test_the_command_quotes_names_a_shell_would_split() -> None:
    """A secret name with a character the shell reads is quoted."""
    use = SecretUse(SecretUseKind.REPOSITORY, uuid.uuid4(), "my repo")

    assert bind_command("dev", "a$b", use) == (
        "lakana secret bind 'a$b' --env dev --repository 'my repo'"
    )
    assert bind_command("dev", "s3", SecretUse.environment()) == (
        "lakana secret bind s3 --env dev --own-env"
    )


def test_a_use_without_its_target_is_refused() -> None:
    """An index or repository use with no id or name could only print an empty target."""
    with pytest.raises(ValueError, match="needs the target's id and name"):
        SecretUse(SecretUseKind.PACKAGE_INDEX, None, "corp")
    with pytest.raises(ValueError, match="needs the target's id and name"):
        SecretUse(SecretUseKind.REPOSITORY, uuid.uuid4(), None)


def test_a_secret_is_not_checked_under_another_environment_name(
    session: Session, principal: PrincipalRef
) -> None:
    """The environment named in the message must be the secret's own."""
    env = _environment(session, "prod")
    secret = _secret(session, env, principal, "s3")

    with pytest.raises(ValueError, match="does not belong to environment 'dev'"):
        require_use(session, secret, SecretUse.environment(), environment_name="dev")


def test_the_command_escapes_a_control_character_in_a_name() -> None:
    """A newline or escape in a name reaches the message as text, not as a control code."""
    use = SecretUse(SecretUseKind.REPOSITORY, uuid.uuid4(), "r\x1b[0m")

    command = bind_command("dev", "a\nb", use)

    assert "\n" not in command
    assert "\x1b" not in command


def test_a_long_non_ascii_name_survives_in_the_command() -> None:
    """A printable name is kept as written, so the command can be pasted and still match."""
    name = "ä" * 220
    use = SecretUse(SecretUseKind.REPOSITORY, uuid.uuid4(), name)

    parts = shlex.split(bind_command("dev", name, use))

    assert parts == ["lakana", "secret", "bind", name, "--env", "dev", "--repository", name]


def test_an_index_secret_refused_as_a_webhook_first_suggests_a_secret_of_its_own(
    session: Session, principal: PrincipalRef
) -> None:
    """Binding it to the environment would break the index, so the text says so."""
    env = _environment(session)
    corp = _index(session, "corp")
    secret = _secret(session, env, principal, "corp-token", SecretUseKind.PACKAGE_INDEX, corp.id)

    with pytest.raises(SecretUseRefused) as caught:
        require_use(session, secret, SecretUse.environment(), environment_name="prod")

    assert str(caught.value) == (
        "Secret 'corp-token' of environment 'prod' is bound to package index 'corp', not to its "
        "environment (storage and webhooks), so Lakana does not use it there. Name a "
        "secret of its own instead; or, if this secret really belongs to the environment, an "
        "administrator binds it, and package index 'corp' then loses it: "
        "lakana secret bind corp-token --env prod --own-env"
    )


def test_a_repository_secret_bound_elsewhere_leads_with_a_secret_of_its_own(
    session: Session, principal: PrincipalRef
) -> None:
    """Moving it would break the other repository, so it is the second suggestion."""
    env = _environment(session)
    first, second = _repository(session, "first"), _repository(session, "second")
    secret = _secret(session, env, principal, "git-token", SecretUseKind.REPOSITORY, first.id)

    with pytest.raises(SecretUseRefused) as caught:
        require_use(session, secret, SecretUse.repository(second), environment_name="prod")

    assert str(caught.value).endswith(
        "Give repository 'second' a secret of its own: `lakana secret create NEW --env prod "
        "--repository second --values-stdin`, then `lakana repo update second "
        "--credential-secret NEW`. Moving this secret instead breaks repository 'first': "
        "lakana secret bind git-token --env prod --repository second"
    )


def test_the_environment_use_has_no_target_row_and_is_named_in_words(
    session: Session,
) -> None:
    """Unbound and environment uses name no row; `resolve_use` builds the environment use."""
    assert target_row(session, SecretUseKind.ENVIRONMENT, uuid.uuid4()) is None
    assert target_row(session, SecretUseKind.PACKAGE_INDEX, None) is None
    assert describe_use(session, None, None) == "no use yet"
    assert describe_use(session, SecretUseKind.ENVIRONMENT, None) == (
        "its environment (storage and webhooks)"
    )
    assert resolve_use(session, SecretUseKind.ENVIRONMENT, None) == SecretUse.environment()


def test_the_approved_options_keep_an_endpoint_without_user_information_as_it_is() -> None:
    options = {"s3.endpoint": "http://seaweedfs:8333", "s3.access-key-id": "secret"}

    assert approved_storage_options(options) == {"s3.endpoint": "http://seaweedfs:8333"}


def test_the_approved_options_drop_user_information_from_the_endpoint() -> None:
    options = {"s3.endpoint": "https://key:pw@host:9000/p", "s3.region": "eu"}

    assert approved_storage_options(options) == {
        "s3.endpoint": "https://host:9000/p",
        "s3.region": "eu",
    }


def test_the_approved_options_without_an_endpoint_hold_the_region_only() -> None:
    assert approved_storage_options({"s3.region": "eu", "s3.secret": "x"}) == {"s3.region": "eu"}
    assert approved_storage_options({}) == {}


def _connection(
    session: Session, environment: Environment, principal: PrincipalRef, name: str = "crm-db"
) -> Connection:
    """Insert a connection row.

    Parameters
    ----------
    session
        Database session.
    environment
        Its environment.
    principal
        Its creator.
    name
        Its name.

    Returns
    -------
    Connection
        The row.
    """
    row = Connection(
        id=uuid.uuid4(),
        environment_id=environment.id,
        name=name,
        kind=ConnectionKind.POSTGRESQL,
        address={"host": "db.example.com", "port": 5432, "database": "crm"},
        settings={"user": "reader", "tls": "verify-full"},
        created_by=principal.id,
    )
    session.add(row)
    session.commit()
    return row


def test_a_connection_use_names_its_option_and_itself() -> None:
    """`--connection <name>` selects it; a message calls it `connection '<name>'`.

    Its URL is the host and port it sends the secret to.
    """
    row = Connection(
        id=uuid.uuid4(),
        name="crm-db",
        kind=ConnectionKind.POSTGRESQL,
        address={"host": "db.example.com", "port": 5432, "database": "crm"},
        settings={"user": "reader"},
    )

    use = SecretUse.connection(row)

    assert (use.kind, use.target_id, use.target_name, use.target_url) == (
        SecretUseKind.CONNECTION,
        row.id,
        "crm-db",
        "db.example.com:5432",
    )
    assert use.option() == "--connection crm-db"
    assert use.wanted() == "connection 'crm-db'"


def test_a_connection_secret_serves_its_connection_and_nothing_else(
    session: Session, principal: PrincipalRef
) -> None:
    """A connection secret serves no index, and an index secret no connection.

    Each refusal holds the command that moves it.
    """
    env = _environment(session)
    corp, crm = _index(session, "corp"), _connection(session, env, principal)
    for_crm = _secret(session, env, principal, "crm-pw", SecretUseKind.CONNECTION, crm.id)
    for_corp = _secret(session, env, principal, "corp-pw", SecretUseKind.PACKAGE_INDEX, corp.id)

    require_use(session, for_crm, SecretUse.connection(crm), environment_name="prod")
    with pytest.raises(SecretUseRefused) as caught:
        require_use(session, for_crm, SecretUse.package_index(corp), environment_name="prod")
    assert "is bound to connection 'crm-db', not to package index 'corp'" in str(caught.value)
    assert bind_command("prod", "crm-pw", SecretUse.package_index(corp)) in str(caught.value)

    with pytest.raises(SecretUseRefused) as caught:
        require_use(session, for_corp, SecretUse.connection(crm), environment_name="prod")
    assert str(caught.value) == (
        "Secret 'corp-pw' of environment 'prod' is bound to package index 'corp', not to "
        "connection 'crm-db', so Lakana does not use it there. Give connection 'crm-db' a "
        "secret of its own: `lakana connection update crm-db --env prod --password-stdin` (or "
        "--values-stdin). Moving this secret instead breaks package index 'corp': "
        + bind_command("prod", "corp-pw", SecretUse.connection(crm))
    )
    with pytest.raises(SecretUseRefused, match="not to its environment"):
        require_use(session, for_crm, SecretUse.environment(), environment_name="prod")


def test_a_removed_connection_is_named_as_removed(
    session: Session, principal: PrincipalRef
) -> None:
    """`describe_use` and `target_row` read a connection like an index."""
    env = _environment(session)
    crm = _connection(session, env, principal)
    secret = _secret(session, env, principal, "crm-pw", SecretUseKind.CONNECTION, crm.id)

    assert target_row(session, SecretUseKind.CONNECTION, crm.id) is crm
    assert describe_use(session, secret.use_kind, secret.use_target_id) == "connection 'crm-db'"
    session.delete(crm)
    session.commit()
    assert describe_use(session, secret.use_kind, secret.use_target_id) == (
        "a connection that was removed"
    )


def test_resolve_use_finds_a_connection_in_the_given_environment_only(
    session: Session, principal: PrincipalRef
) -> None:
    """A connection name means nothing without its environment."""
    prod, dev = _environment(session, "prod"), _environment(session, "dev")
    crm = _connection(session, prod, principal)

    assert resolve_use(
        session, SecretUseKind.CONNECTION, "crm-db", environment_id=prod.id
    ) == SecretUse.connection(crm)
    with pytest.raises(NotFoundError, match="lakana connection list --env dev"):
        resolve_use(session, SecretUseKind.CONNECTION, "crm-db", environment_id=dev.id)
    with pytest.raises(ValueError, match="needs the environment"):
        resolve_use(session, SecretUseKind.CONNECTION, "crm-db")


def test_a_connection_removed_before_its_lock_is_reported(
    session: Session, principal: PrincipalRef
) -> None:
    """`lock_entry` on a connection deleted meanwhile says so instead of binding to nothing."""
    env = _environment(session)
    row = _connection(session, env, principal)
    use = SecretUse.connection(row)
    session.delete(row)
    session.commit()

    with pytest.raises(NotFoundError, match="The connection was removed by someone else"):
        lock_entry(session, use, env)


def test_the_connection_helpers_leave_alone_what_they_do_not_own(
    session: Session, principal: PrincipalRef
) -> None:
    """Each helper changes only a connection that exists and names this very secret.

    `adopt_connection_secret` and `require_connection_takes` do nothing for a connection id that
    names no row (deleted in the same transaction); `detach_from_connection` keeps a connection
    that names another secret.
    """
    env = _environment(session)
    crm = _connection(session, env, principal)
    kept = _secret(session, env, principal, "kept", SecretUseKind.CONNECTION, crm.id)
    crm.secret_id = kept.id
    session.commit()
    gone = uuid.uuid4()
    stray = _secret(session, env, principal, "stray", SecretUseKind.CONNECTION, gone)

    adopt_connection_secret(session, principal, stray, env.name)
    require_connection_takes(
        session,
        SecretUse(SecretUseKind.CONNECTION, gone, "gone"),
        "stray",
        {"password": "x"},
    )
    detach_from_connection(session, stray, crm.id)

    assert crm.secret_id == kept.id
    assert session.get(Connection, gone) is None
