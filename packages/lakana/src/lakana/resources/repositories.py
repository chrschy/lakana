"""The `repositories` resource: `Client.repositories`."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import Repository
from lakana._routes import ROUTES

UPDATABLE = frozenset(
    {"url", "credential_secret", "credential_username", "ssh_known_hosts", "default_ref"}
)
"""Fields `update` may change; mirrors the server."""


def _repository_from_json(data: dict[str, Any]) -> Repository:
    """Build a `Repository` from a `RepositoryOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    Repository
        The repository.
    """
    return Repository(
        id=data["id"],
        name=data["name"],
        kind=data["kind"],
        url=data["url"],
        credential_secret=data["credential_secret"],
        credential_username=data["credential_username"],
        ssh_known_hosts=data["ssh_known_hosts"],
        default_ref=data["default_ref"],
        created_by=data["created_by"],
        created_at=datetime.fromisoformat(data["created_at"]),
        updated_at=datetime.fromisoformat(data["updated_at"]),
    )


_RepositoryList = list[Repository]
"""`list[Repository]` resolved before `Repositories.list` shadows the builtin."""


class Repositories:
    """The `repositories` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(
        self,
        name: str,
        kind: str,
        url: str,
        *,
        credential_secret: str | None = None,
        credential_username: str | None = None,
        ssh_known_hosts: str | None = None,
        default_ref: str | None = None,
    ) -> Repository:
        """Register a repository (needs `MANAGE_REPOSITORY`).

        Parameters
        ----------
        name
            Unique name; no dots.
        kind
            ``local``, ``https`` or ``ssh``.
        url
            URL, or an absolute path for ``local``.
        credential_secret
            Secret name resolved per environment.
        credential_username
            HTTPS username; default ``oauth2``.
        ssh_known_hosts
            Required for ``ssh``.
        default_ref
            Ref deployed when none is given.

        Returns
        -------
        Repository
            The registered repository.

        Raises
        ------
        lakana.errors.Conflict
            If the name is taken.
        lakana.errors.ValidationError
            If a field is invalid.
        """
        response = self._transport.request(
            ROUTES["repositories.create"],
            json={
                "name": name,
                "kind": kind,
                "url": url,
                "credential_secret": credential_secret,
                "credential_username": credential_username,
                "ssh_known_hosts": ssh_known_hosts,
                "default_ref": default_ref,
            },
        )
        return _repository_from_json(response.json())

    def list(self) -> _RepositoryList:
        """List repositories.

        Returns
        -------
        list of Repository
            Sorted by name.
        """
        response = self._transport.request(ROUTES["repositories.list"])
        return [_repository_from_json(item) for item in response.json()]

    def get(self, name: str) -> Repository:
        """Show a repository.

        Parameters
        ----------
        name
            Its name.

        Returns
        -------
        Repository
            The repository.

        Raises
        ------
        lakana.errors.NotFound
            If none has that name.
        """
        response = self._transport.request(ROUTES["repositories.get"], path_params={"name": name})
        return _repository_from_json(response.json())

    def update(self, name: str, **changes: str | None) -> Repository:
        """Change a repository; `None` clears a field.

        Parameters
        ----------
        name
            Its name.
        **changes
            Any of `url`, `credential_secret`, `credential_username`,
            `ssh_known_hosts`, `default_ref`.

        Returns
        -------
        Repository
            The updated repository.

        Raises
        ------
        TypeError
            For any other keyword.
        """
        unknown = set(changes) - UPDATABLE
        if unknown:
            raise TypeError(f"update() got unexpected field(s): {', '.join(sorted(unknown))}")
        response = self._transport.request(
            ROUTES["repositories.update"], path_params={"name": name}, json=changes
        )
        return _repository_from_json(response.json())

    def delete(self, name: str) -> None:
        """Delete a repository.

        Parameters
        ----------
        name
            Its name.

        Raises
        ------
        lakana.errors.Conflict
            While any environment deploys it.
        """
        self._transport.request(ROUTES["repositories.delete"], path_params={"name": name})
