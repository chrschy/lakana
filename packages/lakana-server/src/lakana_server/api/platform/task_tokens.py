"""The task-tokens resource of the platform API."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_server.api.platform.schemas import TaskTokenMintIn, TaskTokenMintOut
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal, get_task_token_service

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/task-tokens", tags=["task-tokens"]
)


@router.post("", response_model=TaskTokenMintOut, status_code=201)
def mint_task_token(
    body: TaskTokenMintIn,
    principal: PrincipalRef = Depends(get_principal),
    service: TaskTokenService = Depends(get_task_token_service),
) -> TaskTokenMintOut:
    """Mint a task token.

    Parameters
    ----------
    body
        The identity, environment, and asset scope to mint for.
    principal
        The authenticated caller.
    service
        The task-token service for this request.

    Returns
    -------
    TaskTokenMintOut
        The full token value — shown here exactly once.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    minted = service.mint(
        principal,
        body.principal,
        environment=body.environment,
        asset_scope=body.asset_scope,
        ttl_seconds=body.ttl_seconds,
    )
    return TaskTokenMintOut(key_id=minted.key_id, token=minted.token)


@router.delete("/{key_id}", status_code=204)
def revoke_task_token(
    key_id: str,
    principal: PrincipalRef = Depends(get_principal),
    service: TaskTokenService = Depends(get_task_token_service),
) -> None:
    """Revoke a task token before it expires; revoking one already revoked is a no-op.

    Parameters
    ----------
    key_id
        The task token's key_id.
    principal
        The authenticated caller.
    service
        The task-token service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.revoke(principal, key_id)
