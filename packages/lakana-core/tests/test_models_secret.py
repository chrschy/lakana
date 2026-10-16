"""Tests for the Secret model's structural constraints."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.models import Environment, Principal, PrincipalType, Secret


def _add_principal(session: Session, name: str) -> Principal:
    """Insert and return a bare principal for foreign-key targets.

    Returns
    -------
    Principal
        The inserted principal.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(principal)
    session.commit()
    return principal


def _add_environment(session: Session, name: str) -> Environment:
    """Insert and return a bare environment for foreign-key targets.

    Returns
    -------
    Environment
        The inserted environment.
    """
    env = Environment(id=uuid.uuid4(), name=name, storage_root=f"file:///tmp/{name}")
    session.add(env)
    session.commit()
    return env


def test_a_well_formed_secret_persists(session: Session) -> None:
    """A secret with all required fields commits cleanly."""
    admin = _add_principal(session, "admin")
    env = _add_environment(session, "dev")
    secret = Secret(
        id=uuid.uuid4(),
        environment_id=env.id,
        name="s3-creds",
        ciphertext=b"\x01\x02\x03",
        nonce=b"\x00" * 12,
        wrapped_data_key=b"\x04\x05\x06",
        format_version=1,
        key_id="env",
        created_by=admin.id,
        created_at=datetime.now(UTC),
    )
    session.add(secret)
    session.commit()
    stored = session.get(Secret, secret.id)
    assert stored is not None
    assert (stored.format_version, stored.key_id) == (1, "env")


def test_a_secret_name_is_unique_per_environment(session: Session) -> None:
    """Two secrets with the same name in the same environment collide."""
    admin = _add_principal(session, "admin")
    env = _add_environment(session, "dev")
    session.add(
        Secret(
            id=uuid.uuid4(),
            environment_id=env.id,
            name="s3-creds",
            ciphertext=b"\x01",
            nonce=b"\x00" * 12,
            wrapped_data_key=b"\x02",
            format_version=1,
            key_id="env",
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()
    session.add(
        Secret(
            id=uuid.uuid4(),
            environment_id=env.id,
            name="s3-creds",
            ciphertext=b"\x03",
            nonce=b"\x00" * 12,
            wrapped_data_key=b"\x04",
            format_version=1,
            key_id="env",
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_the_same_secret_name_is_allowed_in_a_different_environment(session: Session) -> None:
    """The uniqueness constraint is scoped per environment, not global."""
    admin = _add_principal(session, "admin")
    dev = _add_environment(session, "dev")
    prod = _add_environment(session, "prod")
    session.add(
        Secret(
            id=uuid.uuid4(),
            environment_id=dev.id,
            name="s3-creds",
            ciphertext=b"\x01",
            nonce=b"\x00" * 12,
            wrapped_data_key=b"\x02",
            format_version=1,
            key_id="env",
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    session.add(
        Secret(
            id=uuid.uuid4(),
            environment_id=prod.id,
            name="s3-creds",
            ciphertext=b"\x03",
            nonce=b"\x00" * 12,
            wrapped_data_key=b"\x04",
            format_version=1,
            key_id="env",
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert session.query(Secret).count() == 2


def test_environment_gains_a_nullable_storage_secret_ref(session: Session) -> None:
    """storage_secret_ref defaults to None and can be set without a real Secret row.

    No FK is placed on this column (see the model docstring): it is a plain
    name lookup against `(environment_id, name)`, resolved at storage-
    resolution time, not a foreign key — a dangling reference fails closed
    with a clear NotFoundError there instead of a raw IntegrityError here.
    """
    env = Environment(id=uuid.uuid4(), name="dev", storage_root="file:///tmp/dev")
    session.add(env)
    session.commit()
    assert env.storage_secret_ref is None

    env.storage_secret_ref = "does-not-exist-yet"
    session.commit()
    session.refresh(env)
    assert env.storage_secret_ref == "does-not-exist-yet"
