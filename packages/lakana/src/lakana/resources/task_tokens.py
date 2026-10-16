"""The `task_tokens` resource: `Client.task_tokens`."""

from __future__ import annotations

from lakana._http import HttpTransport
from lakana._models import MintedTaskToken
from lakana._routes import ROUTES


class TaskTokens:
    """The `task-tokens` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def mint(
        self, principal: str, environment: str, asset_scope: list[str], ttl_seconds: int
    ) -> MintedTaskToken:
        """Mint a task token.

        Parameters
        ----------
        principal
            The identity the token authenticates as: your own name, unless you
            hold the `ADMIN` role on the instance.
        environment
            The one environment this task may touch.
        asset_scope
            The declared asset name/pattern strings, e.g. `["bronze.*"]`.
        ttl_seconds
            Seconds until the token expires, from now. Capped server-side by
            the deployment's configured maximum.

        Returns
        -------
        MintedTaskToken
            The full token value — shown here exactly once.

        Raises
        ------
        lakana.errors.NotFound
            If no principal of this name exists.
        lakana.errors.PermissionDenied
            If minting for another principal without the `ADMIN` role.
        lakana.errors.ValidationError
            If `asset_scope` contains a malformed pattern, or `ttl_seconds`
            exceeds the deployment's configured maximum.
        """
        response = self._transport.request(
            ROUTES["task_tokens.mint"],
            json={
                "principal": principal,
                "environment": environment,
                "asset_scope": asset_scope,
                "ttl_seconds": ttl_seconds,
            },
        )
        body = response.json()
        return MintedTaskToken(key_id=body["key_id"], token=body["token"])

    def revoke(self, key_id: str) -> None:
        """Revoke a task token before it expires; revoking one already revoked is a no-op.

        Parameters
        ----------
        key_id
            The task token's key_id.

        Raises
        ------
        lakana.errors.NotFound
            If no task token has this key_id.
        lakana.errors.PermissionDenied
            If the caller may not mint task tokens.
        """
        self._transport.request(ROUTES["task_tokens.revoke"], path_params={"key_id": key_id})
