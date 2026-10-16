"""The tokens resource of the platform API, nested under a principal."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.identity.pats import PatService
from lakana_server.api.platform.schemas import TokenIssueIn, TokenIssueOut, TokenOut
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_pat_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/principals/{name}/tokens", tags=["tokens"]
)


@router.post("", response_model=TokenIssueOut, status_code=201)
def issue_token(
    name: str,
    body: TokenIssueIn,
    principal: PrincipalRef = Depends(get_principal),
    service: PatService = Depends(get_pat_service),
) -> TokenIssueOut:
    """Issue a PAT for a principal.

    Parameters
    ----------
    name
        The principal the PAT authenticates as.
    body
        The PAT's label.
    principal
        The authenticated caller.
    service
        The PAT service for this request.

    Returns
    -------
    TokenIssueOut
        The full token value — shown here exactly once.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    issued = service.issue(principal, name, name=body.name)
    return TokenIssueOut(key_id=issued.key_id, name=issued.name, token=issued.token)


@router.get("", response_model=list[TokenOut])
def list_tokens(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: PatService = Depends(get_pat_service),
) -> list[TokenOut]:
    """List every PAT issued to a principal. Never includes a secret.

    Parameters
    ----------
    name
        The principal whose PATs to list.
    principal
        The authenticated caller.
    service
        The PAT service for this request.

    Returns
    -------
    list of TokenOut
        Every PAT for that principal.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    return [TokenOut.model_validate(row) for row in service.list(principal, name)]


@router.delete("/{key_id}", status_code=204)
def revoke_token(
    name: str,
    key_id: str,
    principal: PrincipalRef = Depends(get_principal),
    service: PatService = Depends(get_pat_service),
) -> None:
    """Revoke a PAT.

    Parameters
    ----------
    name
        The principal the PAT belongs to.
    key_id
        The PAT's key_id.
    principal
        The authenticated caller.
    service
        The PAT service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.revoke(principal, name, key_id)
