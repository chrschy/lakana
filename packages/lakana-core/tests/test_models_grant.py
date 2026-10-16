"""Tests for the Grant model's structural constraints."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.auth import Action, SecurableKind
from lakana_core.grants.roles import Role
from lakana_core.models import Grant, Principal, PrincipalType


def _add_principal(session: Session, name: str) -> Principal:
    """Insert and return a bare principal for foreign-key targets.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name.

    Returns
    -------
    Principal
        The persisted principal.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(principal)
    session.commit()
    return principal


def test_a_grant_needs_exactly_one_subject(session: Session) -> None:
    """Setting neither principal_id nor group_id violates the check constraint."""
    admin = _add_principal(session, "admin")
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=None,
            group_id=None,
            role=Role.VIEWER,
            action=None,
            kind=SecurableKind.INSTANCE,
            granted_by=admin.id,
            granted_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_grant_needs_exactly_one_of_role_or_action(session: Session) -> None:
    """Setting both role and action violates the check constraint."""
    admin = _add_principal(session, "admin")
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=admin.id,
            group_id=None,
            role=Role.VIEWER,
            action=Action.READ_TABLE,
            kind=SecurableKind.INSTANCE,
            granted_by=admin.id,
            granted_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_well_formed_grant_persists(session: Session) -> None:
    """A grant with exactly one subject and exactly one of role/action commits cleanly."""
    admin = _add_principal(session, "admin")
    grant = Grant(
        id=uuid.uuid4(),
        principal_id=admin.id,
        group_id=None,
        role=Role.ADMIN,
        action=None,
        kind=SecurableKind.INSTANCE,
        granted_by=admin.id,
        granted_at=datetime.now(UTC),
    )
    session.add(grant)
    session.commit()
    assert session.get(Grant, grant.id) is not None
