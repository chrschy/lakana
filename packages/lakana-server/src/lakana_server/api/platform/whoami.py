"""The whoami resource of the platform API.

Describes the authenticated caller back to itself. It needs authentication
but performs no authorization check: it reveals nothing a 200-versus-401
probe of any other route doesn't already, and `lakana login` uses it to
confirm a credential before saving it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_server.api.platform.schemas import CiTokenScopeOut, TaskTokenScopeOut, WhoamiOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/whoami",
    tags=["whoami"],
    responses=errors(401),
)


@router.get("", response_model=WhoamiOut)
def whoami(principal: PrincipalRef = Depends(get_principal)) -> WhoamiOut:
    """Show who is calling.

    Answers with the name and type of the principal the credential signs in as and, for a
    task or CI token, the scope of that token. Any valid credential may ask.

    Parameters
    ----------
    principal
        The authenticated caller.

    Returns
    -------
    WhoamiOut
        The caller's name and type, plus its task-token scope or its CI token's scope if it
        authenticated with one of those.
    """
    scope = principal.task_scope
    ci = principal.ci_scope
    return WhoamiOut(
        name=principal.name,
        type=principal.type.value,
        task_token=None
        if scope is None
        else TaskTokenScopeOut(environment=scope.environment, asset_scope=list(scope.asset_scope)),
        ci_token=None
        if ci is None
        else CiTokenScopeOut(
            rule_id=ci.rule_id,
            environment=ci.environment,
            repository=ci.repository,
            kinds=sorted(ci.kinds),
            sha=ci.sha,
            expires_at=ci.expires_at,
            used=ci.used,
        ),
    )
