"""The groups resource of the platform API."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.identity.groups import GroupService
from lakana_server.api.platform.schemas import GroupCreate, GroupOut, PrincipalOut
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_group_service, get_principal

router = APIRouter(route_class=AuthenticateFirstRoute, prefix="/api/v1/groups", tags=["groups"])


@router.post("", response_model=GroupOut, status_code=201)
def create_group(
    body: GroupCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: GroupService = Depends(get_group_service),
) -> GroupOut:
    """Create a group.

    Parameters
    ----------
    body
        The group to create.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Returns
    -------
    GroupOut
        The created group.

    Raises
    ------
    lakana_core.errors.AlreadyExistsError
        Mapped to 409 by `install_exception_handlers`.
    """
    created = service.create_group(principal, name=body.name)
    return GroupOut.model_validate(created)


@router.get("", response_model=list[GroupOut])
def list_groups(
    principal: PrincipalRef = Depends(get_principal),
    service: GroupService = Depends(get_group_service),
) -> list[GroupOut]:
    """List every group.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The group service for this request.

    Returns
    -------
    list of GroupOut
        Every group.
    """
    return [GroupOut.model_validate(row) for row in service.list_groups(principal)]


@router.delete("/{name}", status_code=204)
def delete_group(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: GroupService = Depends(get_group_service),
) -> None:
    """Delete a group.

    Parameters
    ----------
    name
        Group name.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.ConflictError
        Mapped to 409 by `install_exception_handlers`, if any active grant,
        row filter, or column mask still references the group.
    """
    service.delete_group(principal, name)


@router.get("/{name}/members", response_model=list[PrincipalOut])
def list_members(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: GroupService = Depends(get_group_service),
) -> list[PrincipalOut]:
    """List a group's members.

    Parameters
    ----------
    name
        The group's name.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Returns
    -------
    list of PrincipalOut
        Every member of the group, ordered by name.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    return [PrincipalOut.model_validate(row) for row in service.list_members(principal, name)]


@router.put("/{name}/members/{principal_name}", status_code=204)
def add_member(
    name: str,
    principal_name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: GroupService = Depends(get_group_service),
) -> None:
    """Add a principal to a group. Idempotent.

    Parameters
    ----------
    name
        Group name.
    principal_name
        Principal to add.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.add_member(principal, name, principal_name)


@router.delete("/{name}/members/{principal_name}", status_code=204)
def remove_member(
    name: str,
    principal_name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: GroupService = Depends(get_group_service),
) -> None:
    """Remove a principal from a group.

    Parameters
    ----------
    name
        Group name.
    principal_name
        Principal to remove.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.remove_member(principal, name, principal_name)
