"""The `principals` resource: `Client.principals`."""

from __future__ import annotations

from typing import Any

from lakana._http import HttpTransport
from lakana._models import Principal
from lakana._routes import ROUTES


def _principal_from_json(data: dict[str, Any]) -> Principal:
    """Build a `Principal` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `PrincipalOut` JSON body.

    Returns
    -------
    Principal
        The parsed principal.
    """
    return Principal(name=data["name"], type=data["type"], is_active=data["is_active"])


_PrincipalList = list[Principal]
"""Alias for `list[Principal]`, resolved at module scope.

`Principals` defines a method named `list`, which shadows the builtin
`list` for any bare `list[...]` annotation written *inside* the class body —
this alias is resolved before that happens, and referenced by name instead.
"""


class Principals:
    """The `principals` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(self, name: str, type: str) -> Principal:
        """Create a principal.

        Parameters
        ----------
        name
            The principal's unique name.
        type
            `"USER"` or `"SERVICE_ACCOUNT"`.

        Returns
        -------
        Principal
            The created principal.

        Raises
        ------
        lakana.errors.Conflict
            If a principal of this name already exists.
        """
        response = self._transport.request(
            ROUTES["principals.create"], json={"name": name, "type": type}
        )
        return _principal_from_json(response.json())

    def get(self, name: str) -> Principal:
        """Fetch a principal by name.

        Parameters
        ----------
        name
            The principal's name.

        Returns
        -------
        Principal
            The principal.

        Raises
        ------
        lakana.errors.NotFound
            If no principal of this name exists.
        """
        response = self._transport.request(ROUTES["principals.get"], path_params={"name": name})
        return _principal_from_json(response.json())

    def list(self) -> _PrincipalList:
        """List every principal.

        Returns
        -------
        list of Principal
            Every principal.
        """
        response = self._transport.request(ROUTES["principals.list"])
        return [_principal_from_json(item) for item in response.json()]

    def deactivate(self, name: str) -> None:
        """Deactivate a principal.

        Sets `is_active = False` rather than deleting the principal,
        preserving every grant, PAT, and audit entry that references it.

        Parameters
        ----------
        name
            The principal's name.

        Raises
        ------
        lakana.errors.NotFound
            If no principal of this name exists.
        """
        self._transport.request(ROUTES["principals.deactivate"], path_params={"name": name})
