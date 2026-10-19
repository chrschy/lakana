"""Effective permissions: `GrantAuthorization.explain` and `explain_permissions` (#147).

The answer must be the authorizer's own: every test compares it with what a route's
check decides (`GrantAuthorization.permits`, or `authorize_admin_only` for the three
actions that need the `ADMIN` role), not only with hand-written expectations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, Securable, SecurableKind, TaskScope
from lakana_core.catalog.identity import resolve_environment_id, resolve_table_id
from lakana_core.errors import PermissionDenied
from lakana_core.grants.admin import ADMIN_ONLY_ACTIONS, authorize_admin_only
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.explain import ACTIONS_AT, explain_permissions
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath
from lakana_core.models import (
    Environment,
    Grant,
    Group,
    GroupMembership,
    Namespace,
    Principal,
    PrincipalType,
    Table,
    TableType,
)

_GOLD = NamespacePath.from_parts(["gold"])
_GOLD_EU = NamespacePath.from_parts(["gold", "eu"])
_SILVER = NamespacePath.from_parts(["silver"])


@dataclass(frozen=True)
class _World:
    """The principals, ids and grants the matrix runs over.

    Parameters
    ----------
    principals
        Every principal by name.
    grants
        The grants by a short label.
    """

    principals: dict[str, Principal]
    grants: dict[str, Grant]


def _ref(row: Principal) -> PrincipalRef:
    """Build the reference a route would act as for a principal row.

    Parameters
    ----------
    row
        The principal.

    Returns
    -------
    PrincipalRef
        Its id, name and type, without a task scope.
    """
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _principal(session: Session, name: str, *, active: bool = True) -> Principal:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.
    active
        Whether it may sign in.

    Returns
    -------
    Principal
        The row.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name, is_active=active)
    session.add(row)
    session.commit()
    return row


def _grant(session: Session, by: uuid.UUID, **fields: object) -> Grant:
    """Persist a grant row directly.

    Parameters
    ----------
    session
        Database session.
    by
        The granting principal's id.
    **fields
        The grant's columns.

    Returns
    -------
    Grant
        The row.
    """
    row = Grant(id=uuid.uuid4(), granted_by=by, granted_at=datetime.now(UTC), **fields)
    session.add(row)
    session.commit()
    return row


def _catalog(session: Session) -> uuid.UUID:
    """Persist environment `dev` with `gold`, `gold.eu`, `silver` and two tables.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The environment's id.
    """
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///dev/null",
        storage_options={},
        properties={},
    )
    session.add(env)
    session.flush()
    spaces = {}
    for path in (_GOLD, _GOLD_EU, _SILVER):
        spaces[path] = Namespace(
            id=uuid.uuid4(), environment_id=env.id, path=path.joined(), properties={}
        )
        session.add(spaces[path])
    session.flush()
    for path, name in ((_GOLD, "orders"), (_SILVER, "visits")):
        session.add(
            Table(
                id=uuid.uuid4(),
                namespace_id=spaces[path].id,
                name=name,
                table_type=TableType.ICEBERG,
            )
        )
    session.commit()
    found = resolve_environment_id(session, "dev")
    assert found is not None
    return found


@pytest.fixture
def world(session: Session) -> _World:
    """Persist the matrix: direct, group and role grants at every level.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    _World
        The principals and grants.
    """
    env_id = _catalog(session)
    orders_id = resolve_table_id(session, "dev", _GOLD.joined(), "orders")
    people = {name: _principal(session, name) for name in ("ada", "alice", "bob", "carol", "erin")}
    people["dave"] = _principal(session, "dave", active=False)
    by = people["ada"].id
    analysts = Group(id=uuid.uuid4(), name="analysts")
    admins = Group(id=uuid.uuid4(), name="admins")
    session.add_all([analysts, admins])
    session.flush()
    session.add_all(
        [
            GroupMembership(group_id=analysts.id, principal_id=people["alice"].id),
            GroupMembership(group_id=admins.id, principal_id=people["bob"].id),
        ]
    )
    session.commit()
    instance = {"kind": SecurableKind.INSTANCE}
    in_dev = {"environment": "dev", "environment_id": env_id}
    grants = {
        "ada-admin": _grant(session, by, principal_id=by, role=Role.ADMIN, **instance),
        "alice-read-orders": _grant(
            session,
            by,
            principal_id=people["alice"].id,
            action=Action.READ_TABLE,
            kind=SecurableKind.TABLE,
            namespace=_GOLD.joined(),
            table="orders",
            table_id=orders_id,
            **in_dev,
        ),
        "analysts-gold": _grant(
            session,
            by,
            group_id=analysts.id,
            role=Role.ANALYST,
            kind=SecurableKind.NAMESPACE,
            namespace=_GOLD.joined(),
            **in_dev,
        ),
        "alice-silver": _grant(
            session,
            by,
            principal_id=people["alice"].id,
            role=Role.DATA_SCIENTIST,
            kind=SecurableKind.NAMESPACE,
            namespace=_SILVER.joined(),
            **in_dev,
        ),
        "alice-deploy": _grant(
            session,
            by,
            principal_id=people["alice"].id,
            action=Action.DEPLOY,
            kind=SecurableKind.ENVIRONMENT,
            **in_dev,
        ),
        "alice-policy": _grant(
            session, by, principal_id=people["alice"].id, action=Action.MANAGE_POLICY, **instance
        ),
        "alice-manage-grant": _grant(
            session, by, principal_id=people["alice"].id, action=Action.MANAGE_GRANT, **instance
        ),
        "admins-admin": _grant(session, by, group_id=admins.id, role=Role.ADMIN, **instance),
        "dave-admin": _grant(
            session, by, principal_id=people["dave"].id, role=Role.ADMIN, **instance
        ),
        "erin-dev": _grant(
            session,
            by,
            principal_id=people["erin"].id,
            role=Role.ADMIN,
            kind=SecurableKind.ENVIRONMENT,
            **in_dev,
        ),
    }
    return _World(people, grants)


_SCOPES = {
    "instance": Securable.instance_scope(),
    "dev": Securable.environment_scope("dev"),
    "gold": Securable.namespace_scope("dev", _GOLD),
    "gold.eu": Securable.namespace_scope("dev", _GOLD_EU),
    "silver": Securable.namespace_scope("dev", _SILVER),
    "gold.orders": Securable.table_scope("dev", _GOLD, "orders"),
    "silver.visits": Securable.table_scope("dev", _SILVER, "visits"),
}


def _route_decides(session: Session, who: PrincipalRef, action: Action, scope: Securable) -> bool:
    """Ask the checks the routes run whether `who` may do `action` on `scope`.

    Parameters
    ----------
    session
        Database session.
    who
        The principal.
    action
        The action.
    scope
        The object.

    Returns
    -------
    bool
        What a route would decide.
    """
    authz = GrantAuthorization(session)
    if action in ADMIN_ONLY_ACTIONS:
        try:
            authorize_admin_only(session, authz, who, action)
        except PermissionDenied:
            return False
        return True
    return authz.permits(who, action, scope)


@pytest.mark.parametrize("scope", sorted(_SCOPES))
@pytest.mark.parametrize("name", ["ada", "alice", "bob", "carol", "dave", "erin"])
def test_explain_agrees_with_the_routes_for_every_action(
    session: Session, world: _World, name: str, scope: str
) -> None:
    """Each answer is what the route's own check decides, for every listed action."""
    row = world.principals[name]
    report = explain_permissions(session, row, _SCOPES[scope])
    assert [p.action for p in report.permissions] == list(ACTIONS_AT[_SCOPES[scope].kind])
    for permission in report.permissions:
        decided = _route_decides(session, _ref(row), permission.action, _SCOPES[scope])
        assert permission.allowed is decided, (name, scope, permission.action)
        assert bool(permission.via) is permission.allowed
        assert (permission.reason is None) is permission.allowed


def test_each_way_a_grant_reaches_a_principal_is_named(session: Session, world: _World) -> None:
    """A direct action, a group's role and a direct role are told apart, with their scope."""
    alice = world.principals["alice"]
    at_orders = explain_permissions(session, alice, _SCOPES["gold.orders"])
    read = next(p for p in at_orders.permissions if p.action is Action.READ_TABLE)
    by_id = {via.grant_id: via for via in read.via}
    assert set(by_id) == {world.grants["alice-read-orders"].id, world.grants["analysts-gold"].id}
    direct = by_id[world.grants["alice-read-orders"].id]
    assert (direct.kind, direct.group, direct.role) == ("grant", None, None)
    assert direct.scope == _SCOPES["gold.orders"]
    via_group = by_id[world.grants["analysts-gold"].id]
    assert (via_group.kind, via_group.group, via_group.role) == ("group", "analysts", Role.ANALYST)
    assert via_group.scope == _SCOPES["gold"]
    write = next(p for p in at_orders.permissions if p.action is Action.WRITE_TABLE)
    assert write.allowed is False
    assert write.reason == "'alice' may not WRITE_TABLE this."

    at_silver = explain_permissions(session, alice, _SCOPES["silver.visits"])
    create = next(p for p in at_silver.permissions if p.action is Action.CREATE_TABLE)
    assert [(v.kind, v.role, v.grant_id) for v in create.via] == [
        ("role", Role.DATA_SCIENTIST, world.grants["alice-silver"].id)
    ]


def test_a_nested_namespace_inherits_its_parents_grant(session: Session, world: _World) -> None:
    """`gold.eu` is covered by the group's grant on `gold`, and says so."""
    report = explain_permissions(session, world.principals["alice"], _SCOPES["gold.eu"])
    read = next(p for p in report.permissions if p.action is Action.READ_NAMESPACE)
    assert [v.grant_id for v in read.via] == [world.grants["analysts-gold"].id]


def test_an_action_that_needs_the_admin_role_is_not_given_by_a_grant_of_it_alone(
    session: Session, world: _World
) -> None:
    """A single-action grant of MANAGE_GRANT has no effect; the ADMIN role does, through a group."""
    alice = explain_permissions(session, world.principals["alice"], _SCOPES["instance"])
    manage = next(p for p in alice.permissions if p.action is Action.MANAGE_GRANT)
    assert manage.allowed is False
    assert manage.via == ()
    assert manage.reason is not None and "needs the ADMIN role on the instance" in manage.reason
    policy = next(p for p in alice.permissions if p.action is Action.MANAGE_POLICY)
    assert [v.grant_id for v in policy.via] == [world.grants["alice-policy"].id]

    bob = explain_permissions(session, world.principals["bob"], _SCOPES["instance"])
    manage = next(p for p in bob.permissions if p.action is Action.MANAGE_GRANT)
    assert [(v.kind, v.group, v.role) for v in manage.via] == [("group", "admins", Role.ADMIN)]


def test_admin_on_an_environment_is_not_admin_on_the_instance(
    session: Session, world: _World
) -> None:
    """`erin` holds ADMIN on `dev`: everything there, nothing on the instance."""
    erin = world.principals["erin"]
    at_dev = explain_permissions(session, erin, _SCOPES["dev"])
    assert all(p.allowed for p in at_dev.permissions)
    at_instance = explain_permissions(session, erin, _SCOPES["instance"])
    assert not any(p.allowed for p in at_instance.permissions)


def test_a_deactivated_principal_is_flagged(session: Session, world: _World) -> None:
    """The authorizer's answer stays, and the report says the principal cannot sign in."""
    report = explain_permissions(session, world.principals["dave"], _SCOPES["dev"])
    assert report.active is False
    assert report.note is not None and "deactivated" in report.note
    assert all(p.allowed for p in report.permissions)
    active = explain_permissions(session, world.principals["alice"], _SCOPES["dev"])
    assert (active.active, active.note) == (True, None)


def test_the_actions_of_each_level_cover_every_action(session: Session) -> None:
    """No action is left out of every level, and each level lists an action once."""
    listed = {action for actions in ACTIONS_AT.values() for action in actions}
    assert listed == set(Action)
    for actions in ACTIONS_AT.values():
        assert len(actions) == len(set(actions))


def test_explain_gives_the_reason_authorize_raises_for_a_task_token(
    session: Session, world: _World
) -> None:
    """A task token's narrowing is part of the decision, with the same message."""
    alice = world.principals["alice"]
    token = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["silver.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    decision = authz.explain(token, Action.READ_TABLE, _SCOPES["gold.orders"])
    with pytest.raises(PermissionDenied) as caught:
        authz.authorize(token, Action.READ_TABLE, _SCOPES["gold.orders"])
    assert decision.allowed is False
    assert decision.grants == ()
    assert decision.reason == str(caught.value)
    allowed = authz.explain(token, Action.READ_TABLE, _SCOPES["silver.visits"])
    assert allowed.allowed is True
    assert [g.id for g in allowed.grants] == [world.grants["alice-silver"].id]
    assert allowed.reason is None


def test_a_revoked_grant_explains_nothing(session: Session, world: _World) -> None:
    """Revoking the only grant turns the answer to refused."""
    grant = world.grants["alice-deploy"]
    grant.revoked_at = datetime.now(UTC)
    session.commit()
    report = explain_permissions(session, world.principals["alice"], _SCOPES["dev"])
    deploy = next(p for p in report.permissions if p.action is Action.DEPLOY)
    assert deploy.allowed is False
    assert session.execute(select(Grant).where(Grant.id == grant.id)).scalar_one() is grant
