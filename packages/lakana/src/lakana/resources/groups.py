"""The `groups` resource: `Client.groups`."""

from __future__ import annotations

from typing import Any

from lakana._http import HttpTransport
from lakana._models import Group, Principal
from lakana._routes import ROUTES
from lakana.resources.principals import _principal_from_json


def _group_from_json(data: dict[str, Any]) -> Group:
    """Build a `Group` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `GroupOut` JSON body.

    Returns
    -------
    Group
        The parsed group.
    """
    return Group(name=data["name"])


_GroupList = list[Group]
"""Alias for `list[Group]`, resolved at module scope.

`Groups` defines a method named `list`, which shadows the builtin `list`
for any bare `list[...]` annotation written *inside* the class body — this
alias is resolved before that happens, and referenced by name instead.
"""

_PrincipalList = list[Principal]
"""Alias for `list[Principal]`, resolved at module scope for the same reason."""


class Groups:
    """The `groups` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(self, name: str) -> Group:
        """Create a group.

        Parameters
        ----------
        name
            The group's unique name.

        Returns
        -------
        Group
            The created group.

        Raises
        ------
        lakana.errors.Conflict
            If a group of this name already exists.
        """
        response = self._transport.request(ROUTES["groups.create"], json={"name": name})
        return _group_from_json(response.json())

    def list(self) -> _GroupList:
        """List every group.

        Returns
        -------
        list of Group
            Every group.
        """
        response = self._transport.request(ROUTES["groups.list"])
        return [_group_from_json(item) for item in response.json()]

    def delete(self, name: str) -> None:
        """Delete a group.

        Parameters
        ----------
        name
            The group's name.

        Raises
        ------
        lakana.errors.NotFound
            If no group of this name exists.
        lakana.errors.Conflict
            If any active grant, row filter, or column mask still
            references the group.
        """
        self._transport.request(ROUTES["groups.delete"], path_params={"name": name})

    def list_members(self, name: str) -> _PrincipalList:
        """List a group's members.

        Parameters
        ----------
        name
            The group's name.

        Returns
        -------
        list of Principal
            Every member of the group, ordered by name.

        Raises
        ------
        lakana.errors.NotFound
            If no group of this name exists.
        """
        response = self._transport.request(
            ROUTES["groups.list_members"], path_params={"name": name}
        )
        return [_principal_from_json(item) for item in response.json()]

    def add_member(self, name: str, principal_name: str) -> None:
        """Add a principal to a group. Idempotent.

        Parameters
        ----------
        name
            The group's name.
        principal_name
            The principal to add.

        Raises
        ------
        lakana.errors.NotFound
            If no group or principal of these names exists.
        """
        self._transport.request(
            ROUTES["groups.add_member"],
            path_params={"name": name, "principal_name": principal_name},
        )

    def remove_member(self, name: str, principal_name: str) -> None:
        """Remove a principal from a group.

        Parameters
        ----------
        name
            The group's name.
        principal_name
            The principal to remove.

        Raises
        ------
        lakana.errors.NotFound
            If no group or principal of these names exists.
        """
        self._transport.request(
            ROUTES["groups.remove_member"],
            path_params={"name": name, "principal_name": principal_name},
        )
