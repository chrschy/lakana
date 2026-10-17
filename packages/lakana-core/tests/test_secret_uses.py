"""`lakana_core.secrets.uses`: which use a secret may serve, and what a refusal says."""

from __future__ import annotations

import shlex
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.models import (
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
    bind_command,
    describe_use,
    require_use,
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
        "package index 'other', so Lakana does not use it there. An administrator who means it "
        "changes its use: lakana secret bind corp-token --env prod --index other"
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
        "environment (storage, webhooks and tasks), so Lakana does not use it there. Name a "
        "secret of its own instead; or, if this secret really belongs to the environment, an "
        "administrator binds it, and package index 'corp' then loses it: "
        "lakana secret bind corp-token --env prod --own-env"
    )
