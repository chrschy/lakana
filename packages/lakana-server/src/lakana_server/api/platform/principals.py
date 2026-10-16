"""The principals resource of the platform API."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import PrincipalType
from lakana_server.api.platform.schemas import PrincipalCreate, PrincipalOut
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal, get_principal_service

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/principals", tags=["principals"]
)


@router.post("", response_model=PrincipalOut, status_code=201)
def create_principal(
    body: PrincipalCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: PrincipalService = Depends(get_principal_service),
) -> PrincipalOut:
    """Create a principal.

    Parameters
    ----------
    body
        The principal to create.
    principal
        The authenticated caller.
    service
        The principal service for this request.

    Returns
    -------
    PrincipalOut
        The created principal.

    Raises
    ------
    lakana_core.errors.AlreadyExistsError
        Mapped to 409 by `install_exception_handlers`.
    """
    created = service.create_principal(principal, name=body.name, type=PrincipalType[body.type])
    return PrincipalOut.model_validate(created)


@router.get("/{name}", response_model=PrincipalOut)
def get_principal_by_name(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: PrincipalService = Depends(get_principal_service),
) -> PrincipalOut:
    """Fetch a principal by name.

    Parameters
    ----------
    name
        Principal name.
    principal
        The authenticated caller.
    service
        The principal service for this request.

    Returns
    -------
    PrincipalOut
        The principal.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    return PrincipalOut.model_validate(service.get_principal_by_name(principal, name))


@router.get("", response_model=list[PrincipalOut])
def list_principals(
    principal: PrincipalRef = Depends(get_principal),
    service: PrincipalService = Depends(get_principal_service),
) -> list[PrincipalOut]:
    """List every principal.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The principal service for this request.

    Returns
    -------
    list of PrincipalOut
        Every principal.
    """
    return [PrincipalOut.model_validate(row) for row in service.list_principals(principal)]


@router.delete("/{name}", status_code=204)
def deactivate_principal(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: PrincipalService = Depends(get_principal_service),
) -> None:
    """Deactivate a principal.

    Sets `is_active = False` rather than deleting the row, preserving every
    grant, PAT, and audit entry that references it.

    Parameters
    ----------
    name
        Principal name.
    principal
        The authenticated caller.
    service
        The principal service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.deactivate_principal(principal, name)
