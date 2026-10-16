"""`holds_instance_admin`: the one question the mint rule asks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import Action, SecurableKind
from lakana_core.grants.admin import holds_instance_admin
from lakana_core.grants.roles import Role
from lakana_core.grants.service import active_instance_admin_ids
from lakana_core.models import (
    Grant,
    Group,
    GroupMembership,
    Principal,
    PrincipalType,
)

pytestmark = pytest.mark.filterwarnings("error")
"""The query must not emit a warning (a cartesian-product SAWarning once did)."""


def _principal(session: Session) -> uuid.UUID:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=f"p-{uuid.uuid4().hex[:8]}")
    session.add(row)
    session.commit()
    return row.id


def _grant(session: Session, granter: uuid.UUID, **fields: object) -> Grant:
    """Persist a grant.

    Parameters
    ----------
    session
        Database session.
    granter
        `granted_by`.
    **fields
        The grant's columns.

    Returns
    -------
    Grant
        The row.
    """
    row = Grant(id=uuid.uuid4(), granted_by=granter, granted_at=datetime.now(UTC), **fields)
    session.add(row)
    session.commit()
    return row


def test_a_direct_instance_admin_grant_counts(session: Session) -> None:
    subject = _principal(session)
    assert holds_instance_admin(session, subject) is False
    grant = _grant(
        session, subject, principal_id=subject, role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    assert holds_instance_admin(session, subject) is True
    assert holds_instance_admin(session, _principal(session)) is False
    grant.revoked_at = datetime.now(UTC)
    grant.revoked_by = subject
    session.commit()
    assert holds_instance_admin(session, subject) is False


def test_an_admin_grant_through_a_group_counts(session: Session) -> None:
    subject = _principal(session)
    group = Group(id=uuid.uuid4(), name="admins")
    session.add(group)
    session.flush()
    _grant(session, subject, group_id=group.id, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    assert holds_instance_admin(session, subject) is False
    session.add(GroupMembership(group_id=group.id, principal_id=subject))
    session.commit()
    assert holds_instance_admin(session, subject) is True


def test_a_revoked_group_grant_does_not_count(session: Session) -> None:
    subject = _principal(session)
    group = Group(id=uuid.uuid4(), name="admins")
    session.add(group)
    session.flush()
    grant = _grant(
        session, subject, group_id=group.id, role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    session.add(GroupMembership(group_id=group.id, principal_id=subject))
    session.commit()
    grant.revoked_at = datetime.now(UTC)
    grant.revoked_by = subject
    session.commit()
    assert holds_instance_admin(session, subject) is False


def test_a_deleted_group_does_not_count(session: Session) -> None:
    subject = _principal(session)
    group = Group(id=uuid.uuid4(), name="admins")
    session.add(group)
    session.flush()
    _grant(session, subject, group_id=group.id, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    session.add(GroupMembership(group_id=group.id, principal_id=subject))
    session.commit()
    assert holds_instance_admin(session, subject) is True
    session.delete(group)
    session.commit()
    assert holds_instance_admin(session, subject) is False


def test_a_deactivated_principal_does_not_count(session: Session) -> None:
    subject = _principal(session)
    _grant(session, subject, principal_id=subject, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    assert holds_instance_admin(session, subject) is True
    row = session.get(Principal, subject)
    assert row is not None
    row.is_active = False
    session.commit()
    assert holds_instance_admin(session, subject) is False


@pytest.mark.parametrize(
    "fields",
    [
        {"role": Role.ADMIN, "kind": SecurableKind.ENVIRONMENT, "environment": "dev"},
        {
            "role": Role.ADMIN,
            "kind": SecurableKind.NAMESPACE,
            "environment": "dev",
            "namespace": "a",
        },
        {"role": Role.DATA_ENGINEER, "kind": SecurableKind.INSTANCE},
        {"action": Action.MINT_TASK_TOKEN, "kind": SecurableKind.INSTANCE},
        {"action": Action.MANAGE_GRANT, "kind": SecurableKind.INSTANCE},
    ],
)
def test_nothing_less_than_the_admin_role_at_instance_scope_counts(
    session: Session, fields: dict[str, object]
) -> None:
    subject = _principal(session)
    _grant(session, subject, principal_id=subject, **fields)
    assert holds_instance_admin(session, subject) is False


def test_a_principal_removed_from_the_admin_group_stops_counting(session: Session) -> None:
    subject = _principal(session)
    group = Group(id=uuid.uuid4(), name="admins")
    session.add(group)
    session.flush()
    _grant(session, subject, group_id=group.id, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    membership = GroupMembership(group_id=group.id, principal_id=subject)
    session.add(membership)
    session.commit()
    assert holds_instance_admin(session, subject) is True
    session.delete(membership)
    session.commit()
    assert holds_instance_admin(session, subject) is False


def test_it_agrees_with_active_instance_admin_ids(session: Session) -> None:
    """The two statements of "instance admin" must not drift apart."""
    group = Group(id=uuid.uuid4(), name="admins")
    session.add(group)
    session.flush()
    direct, via_group, revoked, scoped, deactivated, plain = (_principal(session) for _ in range(6))
    _grant(session, direct, principal_id=direct, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    _grant(session, via_group, group_id=group.id, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    session.add(GroupMembership(group_id=group.id, principal_id=via_group))
    gone = _grant(
        session, revoked, principal_id=revoked, role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    gone.revoked_at = datetime.now(UTC)
    gone.revoked_by = revoked
    _grant(
        session,
        scoped,
        principal_id=scoped,
        role=Role.ADMIN,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    _grant(
        session, deactivated, principal_id=deactivated, role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    row = session.get(Principal, deactivated)
    assert row is not None
    row.is_active = False
    session.commit()
    everyone = [direct, via_group, revoked, scoped, deactivated, plain]
    admins = active_instance_admin_ids(session)
    assert admins == {direct, via_group}
    for principal_id in everyone:
        assert holds_instance_admin(session, principal_id) is (principal_id in admins)
