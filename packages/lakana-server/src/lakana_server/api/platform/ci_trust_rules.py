"""The CI trust rules resource of the platform API: instance administrators only.

A rule says which CI jobs (by their OIDC ID token's claims) may deploy which repository to
which environment, as which service account. `lakana_core.ci_oidc.rules` has the rules.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.ci_oidc.rules import CiTrustRuleService, CiTrustRuleView
from lakana_server.api.platform.schemas import (
    CiTrustExplainIn,
    CiTrustExplainOut,
    CiTrustRuleCreate,
    CiTrustRuleOut,
    CiTrustRuleUpdate,
)
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_ci_trust_rule_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/ci-trust-rules", tags=["ci-trust-rules"]
)


def _out(view: CiTrustRuleView) -> CiTrustRuleOut:
    """Build a response body.

    Parameters
    ----------
    view
        The rule.

    Returns
    -------
    CiTrustRuleOut
        The body.
    """
    return CiTrustRuleOut.model_validate(view)


@router.post("", response_model=CiTrustRuleOut, status_code=201)
def create_ci_trust_rule(
    body: CiTrustRuleCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustRuleOut:
    """Create an enabled CI trust rule.

    Parameters
    ----------
    body
        The rule.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustRuleOut
        The rule.
    """
    return _out(
        service.create(
            principal,
            issuer=body.issuer,
            principal=body.principal,
            environment=body.environment,
            repository=body.repository,
            kinds=body.kinds,
            conditions=body.conditions,
        )
    )


@router.get("", response_model=list[CiTrustRuleOut])
def list_ci_trust_rules(
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> list[CiTrustRuleOut]:
    """List every CI trust rule, oldest first.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    list of CiTrustRuleOut
        The rules.
    """
    return [_out(view) for view in service.list(principal)]


@router.post("/explain", response_model=CiTrustExplainOut)
def explain_ci_trust_rules(
    body: CiTrustExplainIn,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustExplainOut:
    """Say which rule an exchange with these claims would match, and why not the others.

    Parameters
    ----------
    body
        The claims and the environment and repository the exchange would ask for.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustExplainOut
        The verdict and every rule's reasons.
    """
    report = service.explain(
        principal,
        claims=body.claims,
        environment=body.environment,
        repository=body.repository,
        issuer=body.issuer,
    )
    return CiTrustExplainOut.model_validate(report)


@router.get("/{rule_id}", response_model=CiTrustRuleOut)
def get_ci_trust_rule(
    rule_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustRuleOut:
    """Show one CI trust rule.

    Parameters
    ----------
    rule_id
        The rule.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustRuleOut
        The rule.
    """
    return _out(service.get(principal, rule_id))


@router.patch("/{rule_id}", response_model=CiTrustRuleOut)
def update_ci_trust_rule(
    rule_id: uuid.UUID,
    body: CiTrustRuleUpdate,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustRuleOut:
    """Change a rule's principal, kinds or conditions; the tokens issued under it end.

    Parameters
    ----------
    rule_id
        The rule.
    body
        Only the fields present change.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustRuleOut
        The rule.
    """
    return _out(
        service.update(
            principal,
            rule_id,
            principal=body.principal,
            kinds=body.kinds,
            conditions=body.conditions,
        )
    )


@router.post("/{rule_id}/enable", response_model=CiTrustRuleOut)
def enable_ci_trust_rule(
    rule_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustRuleOut:
    """Enable a rule.

    Parameters
    ----------
    rule_id
        The rule.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustRuleOut
        The rule.
    """
    return _out(service.enable(principal, rule_id))


@router.post("/{rule_id}/disable", response_model=CiTrustRuleOut)
def disable_ci_trust_rule(
    rule_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustRuleOut:
    """Disable a rule: it matches no exchange, and the tokens issued under it end.

    Parameters
    ----------
    rule_id
        The rule.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustRuleOut
        The rule.
    """
    return _out(service.disable(principal, rule_id))


@router.delete("/{rule_id}", status_code=204)
def delete_ci_trust_rule(
    rule_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> None:
    """Delete a rule; the tokens issued under it end.

    Parameters
    ----------
    rule_id
        The rule.
    principal
        The authenticated caller.
    service
        The rule service.
    """
    service.delete(principal, rule_id)
