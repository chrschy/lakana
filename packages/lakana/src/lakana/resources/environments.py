"""The `environments` resource: `Client.environments`."""

from __future__ import annotations

from typing import Any

from lakana._http import HttpTransport
from lakana._models import Environment
from lakana._routes import ROUTES


def _environment_from_json(data: dict[str, Any]) -> Environment:
    """Build an `Environment` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `EnvironmentOut` JSON body.

    Returns
    -------
    Environment
        The parsed environment.
    """
    return Environment(
        name=data["name"],
        storage_root=data["storage_root"],
        storage_secret_ref=data["storage_secret_ref"],
        is_protected=data["is_protected"],
        properties=data["properties"],
        storage_options_digest=data.get("storage_options_digest"),
    )


_EnvironmentList = list[Environment]
"""Alias for `list[Environment]`, resolved at module scope.

`Environments` defines a method named `list`, which shadows the builtin
`list` for any bare `list[...]` annotation written *inside* the class body —
this alias is resolved before that happens, and referenced by name instead.
"""


class Environments:
    """The `environments` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(self, name: str, storage: dict[str, Any]) -> Environment:
        """Create an environment.

        Parameters
        ----------
        name
            The environment's unique name.
        storage
            Storage configuration: `{"scheme", "root", "options"}`.

        Returns
        -------
        Environment
            The created environment.

        Raises
        ------
        lakana.errors.Conflict
            If an environment of this name already exists.
        """
        response = self._transport.request(
            ROUTES["environments.create"], json={"name": name, "storage": storage}
        )
        return _environment_from_json(response.json())

    def get(self, name: str) -> Environment:
        """Fetch an environment by name.

        Parameters
        ----------
        name
            The environment's name.

        Returns
        -------
        Environment
            The environment.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        """
        response = self._transport.request(ROUTES["environments.get"], path_params={"name": name})
        return _environment_from_json(response.json())

    def list(self) -> _EnvironmentList:
        """List every environment the caller may read.

        Returns
        -------
        list of Environment
            Every readable environment.
        """
        response = self._transport.request(ROUTES["environments.list"])
        return [_environment_from_json(item) for item in response.json()]

    def delete(self, name: str) -> None:
        """Delete an environment.

        Parameters
        ----------
        name
            The environment's name.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        lakana.errors.Conflict
            If a repository is still deployed to it; undeploy first.
        """
        self._transport.request(ROUTES["environments.delete"], path_params={"name": name})

    def update_storage(
        self, name: str, options: dict[str, str] | None = None, secret_ref: str | None = None
    ) -> Environment:
        """Update an environment's storage options and/or secret reference.

        Parameters
        ----------
        name
            The environment's name.
        options
            Replaces the environment's storage options entirely, if given.
        secret_ref
            Replaces the environment's storage secret reference, if given.

        Returns
        -------
        Environment
            The updated environment.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        """
        response = self._transport.request(
            ROUTES["environments.update_storage"],
            path_params={"name": name},
            json={"options": options, "secret_ref": secret_ref},
        )
        return _environment_from_json(response.json())

    def set_protection(self, name: str, protected: bool) -> Environment:
        """Toggle an environment's protected flag.

        Parameters
        ----------
        name
            The environment's name.
        protected
            The new `is_protected` value.

        Returns
        -------
        Environment
            The updated environment.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        """
        response = self._transport.request(
            ROUTES["environments.set_protection"],
            path_params={"name": name},
            json={"is_protected": protected},
        )
        return _environment_from_json(response.json())
