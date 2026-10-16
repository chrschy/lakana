"""Model additions: the package index registry, its action and its audit events."""

from __future__ import annotations

import uuid
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.auth import Action
from lakana_core.grants.authorization import BOUND_TASK_TOKEN_ACTIONS, TASK_TOKEN_ACTIONS
from lakana_core.grants.roles import ROLE_ACTIONS, Role
from lakana_core.models import AuditEventType, Grant, PackageIndex, Principal, PrincipalType


def _principal(session: Session) -> Principal:
    """Insert a principal.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    Principal
        The inserted row.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="ada", is_active=True)
    session.add(principal)
    session.commit()
    return principal


def test_an_index_keeps_its_fields_and_survives_its_creator(session: Session) -> None:
    principal = _principal(session)
    session.add(
        PackageIndex(
            id=uuid.uuid4(),
            name="team",
            url="https://pkgs.example.org/simple",
            credential_secret="team-index",
            created_by=principal.id,
        )
    )
    session.commit()
    row = session.query(PackageIndex).one()
    assert (row.name, row.url, row.credential_secret, row.created_by) == (
        "team",
        "https://pkgs.example.org/simple",
        "team-index",
        principal.id,
    )
    assert row.created_at.tzinfo is not None
    assert row.updated_at.tzinfo is not None
    session.delete(principal)
    session.commit()
    session.refresh(row)
    assert row.created_by is None


def test_a_name_and_a_url_are_each_unique(session: Session) -> None:
    session.add(PackageIndex(id=uuid.uuid4(), name="team", url="https://a.example.org/simple"))
    session.commit()
    for name, url in (
        ("team", "https://b.example.org/simple"),
        ("other", "https://a.example.org/simple"),
    ):
        session.add(PackageIndex(id=uuid.uuid4(), name=name, url=url))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_the_action_goes_only_to_admin_and_never_to_a_task_token() -> None:
    assert Action.MANAGE_PACKAGE_INDEX.value == "MANAGE_PACKAGE_INDEX"
    assert Action.MANAGE_PACKAGE_INDEX not in TASK_TOKEN_ACTIONS
    assert Action.MANAGE_PACKAGE_INDEX not in BOUND_TASK_TOKEN_ACTIONS
    assert Action.MANAGE_PACKAGE_INDEX in ROLE_ACTIONS[Role.ADMIN]
    for role in Role:
        if role is not Role.ADMIN:
            assert Action.MANAGE_PACKAGE_INDEX not in ROLE_ACTIONS[role]


def test_the_grants_column_holds_the_longest_action() -> None:
    assert (
        max(len(a.name) for a in Action)
        == 20
        == cast(sa.String, Grant.__table__.c.action.type).length
    )


def test_the_three_audit_event_types_exist() -> None:
    for name in ("PACKAGE_INDEX_CREATED", "PACKAGE_INDEX_UPDATED", "PACKAGE_INDEX_DELETED"):
        assert AuditEventType[name].value == name
    assert max(len(t.value) for t in AuditEventType) <= 64
