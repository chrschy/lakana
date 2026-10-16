"""The `namespaces` resource: `Client.namespaces`.

Namespace paths are dot-joined strings, such as ``gold.sales``, matching
`lakana_server.api.platform.namespaces`'s own encoding.
"""

from __future__ import annotations

from typing import Any

from lakana._http import HttpTransport
from lakana._models import Namespace
from lakana._routes import ROUTES


def _namespace_from_json(data: dict[str, Any]) -> Namespace:
    """Build a `Namespace` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `NamespaceOut` JSON body.

    Returns
    -------
    Namespace
        The parsed namespace.
    """
    return Namespace(path=data["path"], properties=data["properties"])


_NamespaceList = list[Namespace]
"""Alias for `list[Namespace]`, resolved at module scope.

`Namespaces` defines a method named `list`, which shadows the builtin
`list` for any bare `list[...]` annotation written *inside* the class body —
this alias is resolved before that happens, and referenced by name instead.
"""


class Namespaces:
    """The `namespaces` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(
        self,
        environment: str,
        namespace: str,
        properties: dict[str, str] | None = None,
        *,
        break_glass_reason: str | None = None,
    ) -> Namespace:
        """Create a namespace.

        Parameters
        ----------
        environment
            The environment name.
        namespace
            Dot-joined namespace path, such as `"gold.sales"`.
        properties
            Free-form metadata.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.

        Returns
        -------
        Namespace
            The created namespace.

        Raises
        ------
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.Conflict
            If a namespace of this path already exists.
        """
        body: dict[str, object] = {"path": namespace, "properties": properties or {}}
        if break_glass_reason is not None:
            body["break_glass_reason"] = break_glass_reason
        response = self._transport.request(
            ROUTES["namespaces.create"], path_params={"env": environment}, json=body
        )
        return _namespace_from_json(response.json())

    def get(self, environment: str, namespace: str) -> Namespace:
        """Fetch a namespace by its dotted path.

        Parameters
        ----------
        environment
            The environment name.
        namespace
            Dot-joined namespace path.

        Returns
        -------
        Namespace
            The namespace.

        Raises
        ------
        lakana.errors.NotFound
            If no such namespace exists.
        """
        response = self._transport.request(
            ROUTES["namespaces.get"], path_params={"env": environment, "path": namespace}
        )
        return _namespace_from_json(response.json())

    def list(self, environment: str) -> _NamespaceList:
        """List every namespace in an environment.

        Parameters
        ----------
        environment
            The environment name.

        Returns
        -------
        list of Namespace
            Every namespace in the environment.

        Raises
        ------
        lakana.errors.NotFound
            If the environment does not exist.
        """
        response = self._transport.request(
            ROUTES["namespaces.list"], path_params={"env": environment}
        )
        return [_namespace_from_json(item) for item in response.json()]

    def delete(
        self, environment: str, namespace: str, *, break_glass_reason: str | None = None
    ) -> None:
        """Drop a namespace.

        Parameters
        ----------
        environment
            The environment name.
        namespace
            Dot-joined namespace path.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.

        Raises
        ------
        lakana.errors.NotFound
            If no such namespace exists.
        lakana.errors.Conflict
            If the namespace still contains tables or child namespaces.
        """
        self._transport.request(
            ROUTES["namespaces.delete"],
            path_params={"env": environment, "path": namespace},
            params={"break_glass_reason": break_glass_reason}
            if break_glass_reason is not None
            else None,
        )
