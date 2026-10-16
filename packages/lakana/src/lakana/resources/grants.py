"""The `grants` resource: `Client.grants`.

`namespace` crosses the wire dotted (`gold.sales`) already —
`lakana_server.api.platform.grants` converts to and from its own internal
storage form itself, so the SDK never needs to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lakana._http import HttpTransport, query_params
from lakana._models import Grant
from lakana._routes import ROUTES


def _grant_from_json(data: dict[str, Any]) -> Grant:
    """Build a `Grant` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `GrantOut` JSON body.

    Returns
    -------
    Grant
        The parsed grant.
    """
    return Grant(
        id=data["id"],
        principal_id=data["principal_id"],
        group_id=data["group_id"],
        role=data["role"],
        action=data["action"],
        kind=data["kind"],
        environment=data["environment"],
        namespace=data["namespace"],
        table=data["table"],
        granted_by=data["granted_by"],
        granted_at=datetime.fromisoformat(data["granted_at"]),
        note=data.get("note"),
    )


_GrantList = list[Grant]
"""Alias for `list[Grant]`, resolved at module scope.

`Grants` defines a method named `list`, which shadows the builtin `list`
for any bare `list[...]` annotation written *inside* the class body — this
alias is resolved before that happens, and referenced by name instead.
"""


class Grants:
    """The `grants` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def grant(
        self,
        *,
        principal: str | None = None,
        group: str | None = None,
        role: str | None = None,
        action: str | None = None,
        kind: str,
        environment: str | None = None,
        namespace: str | None = None,
        table: str | None = None,
    ) -> Grant:
        """Create a grant.

        Exactly one of `principal`/`group` and exactly one of `role`/
        `action` must be given; the server raises `ValidationError`
        otherwise. `environment`/`namespace`/`table` must match what `kind`
        expects.

        Parameters
        ----------
        principal
            Name of the principal to grant to, or `None` if granting to a
            group.
        group
            Name of the group to grant to, or `None` if granting to a
            principal.
        role
            A role template to grant, or `None` if granting a raw action.
        action
            A raw action to grant, or `None` if granting a role.
        kind
            Which tier of the securable hierarchy this grant applies to:
            `"INSTANCE"`, `"ENVIRONMENT"`, `"NAMESPACE"`, or `"TABLE"`.
        environment
            The environment, for an ENVIRONMENT/NAMESPACE/TABLE-scoped
            grant.
        namespace
            The dot-joined namespace path, such as `"gold.sales"`, for a
            NAMESPACE/TABLE-scoped grant.
        table
            The table name, for a TABLE-scoped grant.

        Returns
        -------
        Grant
            The created grant.

        Raises
        ------
        lakana.errors.ValidationError
            If the given fields don't form a valid combination.
        lakana.errors.NotFound
            If the named principal, group, or securable does not exist.
        """
        response = self._transport.request(
            ROUTES["grants.create"],
            json={
                "principal": principal,
                "group": group,
                "role": role,
                "action": action,
                "kind": kind,
                "environment": environment,
                "namespace": namespace,
                "table": table,
            },
        )
        return _grant_from_json(response.json())

    def list(
        self,
        *,
        principal_name: str | None = None,
        group_name: str | None = None,
        environment: str | None = None,
        namespace: str | None = None,
        table: str | None = None,
    ) -> _GrantList:
        """List grants for a principal, a group, or a specific securable.

        Exactly one of `principal_name`/`group_name`/a securable filter
        should be given, matching
        `lakana_server.api.platform.grants.list_grants`'s own rules.

        Parameters
        ----------
        principal_name
            List this principal's own direct grants.
        group_name
            List this group's own direct grants.
        environment
            The environment to match, for a securable-scoped listing.
        namespace
            The dot-joined namespace to match, for a securable-scoped
            listing.
        table
            The table to match, for a securable-scoped listing.

        Returns
        -------
        list of Grant
            The matching grants.
        """
        response = self._transport.request(
            ROUTES["grants.list"],
            params=query_params(
                principal_name=principal_name,
                group_name=group_name,
                environment=environment,
                namespace=namespace,
                table=table,
            ),
        )
        return [_grant_from_json(item) for item in response.json()]

    def revoke(self, grant_id: str) -> None:
        """Revoke a grant.

        Parameters
        ----------
        grant_id
            The grant's id.

        Raises
        ------
        lakana.errors.NotFound
            If no grant of this id exists.
        lakana.errors.Conflict
            If revoking this grant would leave zero active instance admins.
        """
        self._transport.request(ROUTES["grants.revoke"], path_params={"grant_id": grant_id})
