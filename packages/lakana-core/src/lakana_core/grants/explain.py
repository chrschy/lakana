"""What a principal may do on one object, and through which grant: its effective permissions.

Every answer is `GrantAuthorization.explain`, the decision `authorize` itself is made of, so an
explanation cannot differ from what a route decides. The three actions that work only with the
`ADMIN` role on the instance add the same role check their call sites make
(`lakana_core.grants.admin.authorize_admin_only`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, Securable, SecurableKind
from lakana_core.grants.admin import admin_only_message, holds_instance_admin, needs_admin_role
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath
from lakana_core.models import Grant, Group, GroupMembership, Principal

_ENVIRONMENT_ACTIONS = (
    Action.READ_ENVIRONMENT,
    Action.READ_NAMESPACE,
    Action.SUBMIT_QUERY,
    Action.MANAGE_ENVIRONMENT,
    Action.MANAGE_SECRET,
    Action.DEPLOY,
    Action.RUN,
)
_NAMESPACE_ACTIONS = (
    Action.READ_NAMESPACE,
    Action.CREATE_NAMESPACE,
    Action.MANAGE_NAMESPACE,
    Action.DROP_NAMESPACE,
    Action.CREATE_TABLE,
)
_TABLE_ACTIONS = (
    Action.READ_TABLE,
    Action.WRITE_TABLE,
    Action.CREATE_TABLE,
    Action.DROP_TABLE,
    Action.MAINTAIN_TABLE,
)
_ONLY_BELOW_THE_INSTANCE = frozenset(_ENVIRONMENT_ACTIONS + _NAMESPACE_ACTIONS + _TABLE_ACTIONS) - {
    Action.MANAGE_ENVIRONMENT
}

ACTIONS_AT: dict[SecurableKind, tuple[Action, ...]] = {
    SecurableKind.INSTANCE: tuple(a for a in Action if a not in _ONLY_BELOW_THE_INSTANCE),
    SecurableKind.ENVIRONMENT: _ENVIRONMENT_ACTIONS,
    SecurableKind.NAMESPACE: _NAMESPACE_ACTIONS,
    SecurableKind.TABLE: _TABLE_ACTIONS,
}
"""The actions an explanation lists for an object of each kind: those the routes ask for there.

`MANAGE_ENVIRONMENT` is asked for on the instance (creating an environment) and on an
environment (changing it). Every action is listed at one level at least.
"""

DEACTIVATED_NOTE = (
    "This principal is deactivated: its tokens no longer work, so it can do none of this. The "
    "answers show what its grants would allow."
)
"""The note of an explanation for a deactivated principal."""


@dataclass(frozen=True)
class Via:
    """One grant that gives a permission, and how it reaches the principal.

    Parameters
    ----------
    kind
        ``grant`` for a single action granted to the principal, ``role`` for a role granted to
        it, ``group`` for either granted to a group it is a member of.
    grant_id
        The grant's id, as `lakana grant revoke` takes it.
    group
        The group's name, for ``group``; else `None`.
    role
        The role granted, or `None` for a single action.
    scope
        The object the grant is on: the one asked about or an ancestor of it.
    """

    kind: Literal["grant", "role", "group"]
    grant_id: uuid.UUID
    group: str | None
    role: Role | None
    scope: Securable


@dataclass(frozen=True)
class Permission:
    """Whether a principal may do one action, and why.

    Parameters
    ----------
    action
        The action.
    allowed
        The authorizer's answer.
    via
        The grants that give it, oldest first; empty when refused.
    reason
        Why it is refused, or `None` when allowed.
    """

    action: Action
    allowed: bool
    via: tuple[Via, ...]
    reason: str | None


@dataclass(frozen=True)
class PermissionReport:
    """A principal's effective permissions on one object.

    Parameters
    ----------
    principal
        The principal's name.
    active
        Whether it may sign in.
    scope
        The object asked about.
    permissions
        One per action of `ACTIONS_AT` for the object's kind, in that order.
    note
        `DEACTIVATED_NOTE` for a deactivated principal, else `None`.
    """

    principal: str
    active: bool
    scope: Securable
    permissions: tuple[Permission, ...]
    note: str | None


def grant_scope(grant: Grant) -> Securable:
    """Say what object a grant is on.

    Parameters
    ----------
    grant
        A grant row.

    Returns
    -------
    Securable
        Its kind and names, the namespace parsed from its storage form.
    """
    return Securable(
        kind=grant.kind,
        environment=grant.environment,
        namespace=NamespacePath.parse(grant.namespace) if grant.namespace is not None else None,
        table=grant.table,
    )


def _via(grant: Grant, groups: dict[uuid.UUID, str]) -> Via:
    """Describe how one grant reaches the principal.

    Parameters
    ----------
    grant
        A grant that covers the action.
    groups
        The names of the principal's groups, by id.

    Returns
    -------
    Via
        The grant, its kind of path, its group and role, and its object.
    """
    kind: Literal["grant", "role", "group"]
    if grant.group_id is not None:
        kind = "group"
    elif grant.role is not None:
        kind = "role"
    else:
        kind = "grant"
    group = groups.get(grant.group_id) if grant.group_id is not None else None
    return Via(kind, grant.id, group, grant.role, grant_scope(grant))


def _permission(
    session: Session,
    authz: GrantAuthorization,
    principal: PrincipalRef,
    action: Action,
    scope: Securable,
    groups: dict[uuid.UUID, str],
) -> Permission:
    """Answer for one action as the route's check would.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorizer.
    principal
        The principal, as a route would act for it.
    action
        The action.
    scope
        The object.
    groups
        The names of the principal's groups, by id.

    Returns
    -------
    Permission
        The decision. For an action that needs the `ADMIN` role, also refused without the
        role on the instance (`admin_only_message`), and given only by that role's grants.
    """
    decision = authz.explain(principal, action, scope)
    grants, reason, allowed = decision.grants, decision.reason, decision.allowed
    if allowed and needs_admin_role(action):
        if holds_instance_admin(session, principal.id):
            grants = tuple(
                g for g in grants if g.role is Role.ADMIN and g.kind is SecurableKind.INSTANCE
            )
        else:
            allowed, grants, reason = False, (), admin_only_message(action)
    return Permission(action, allowed, tuple(_via(g, groups) for g in grants), reason)


def explain_permissions(
    session: Session, principal: Principal, scope: Securable
) -> PermissionReport:
    """Say what a principal may do on an object, action by action, and through which grants.

    Checks nobody's right to ask; `GrantService.explain` does.

    Parameters
    ----------
    session
        Database session.
    principal
        The principal.
    scope
        The object; one that does not exist is answered like any object no grant names.

    Returns
    -------
    PermissionReport
        One permission per action of `ACTIONS_AT` for the object's kind.
    """
    authz = GrantAuthorization(session)
    ref = PrincipalRef(id=principal.id, name=principal.name, type=principal.type)
    member_of = select(GroupMembership.group_id).where(GroupMembership.principal_id == principal.id)
    groups = {
        group_id: name
        for group_id, name in session.execute(
            select(Group.id, Group.name).where(Group.id.in_(member_of))
        )
    }
    permissions = tuple(
        _permission(session, authz, ref, action, scope, groups) for action in ACTIONS_AT[scope.kind]
    )
    return PermissionReport(
        principal=principal.name,
        active=principal.is_active,
        scope=scope,
        permissions=permissions,
        note=None if principal.is_active else DEACTIVATED_NOTE,
    )
