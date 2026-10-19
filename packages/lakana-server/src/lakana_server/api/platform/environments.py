"""The environments resource of the platform API."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_server.api.platform.schemas import (
    EnvironmentCreate,
    EnvironmentOut,
    EnvironmentPatch,
    EnvironmentRetentionIn,
    EnvironmentStorageUpdate,
)
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_catalog, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/environments", tags=["environments"]
)


@router.post("", response_model=EnvironmentOut, status_code=201)
def create_environment(
    body: EnvironmentCreate,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Create an environment.

    Parameters
    ----------
    body
        The environment to create.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The created environment.

    Raises
    ------
    lakana_core.errors.AlreadyExistsError
        Mapped to 409 by `install_exception_handlers`.
    """
    env = catalog.create_environment(
        principal,
        name=body.name,
        storage=StorageConfig(
            scheme=body.storage.scheme, root=body.storage.root, options=body.storage.options
        ),
        is_protected=body.is_protected,
        properties=body.properties,
    )
    return EnvironmentOut.model_validate(env)


@router.patch("/{name}/storage", response_model=EnvironmentOut)
def update_environment_storage(
    name: str,
    body: EnvironmentStorageUpdate,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Update an environment's storage options and/or secret reference.

    Parameters
    ----------
    name
        Environment name.
    body
        The fields to update; each is left unchanged if omitted.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The updated environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    env = catalog.update_environment_storage(
        principal, name, options=body.options, secret_ref=body.secret_ref
    )
    return EnvironmentOut.model_validate(env)


@router.patch("/{name}", response_model=EnvironmentOut)
def set_environment_protection(
    name: str,
    body: EnvironmentPatch,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Toggle an environment's protected flag.

    Parameters
    ----------
    name
        Environment name.
    body
        The new `is_protected` value.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The updated environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    env = catalog.set_environment_protection(principal, name, body.is_protected)
    return EnvironmentOut.model_validate(env)


@router.put("/{name}/retention", response_model=EnvironmentOut)
def set_environment_retention(
    name: str,
    body: EnvironmentRetentionIn,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Set the retention of an environment's tables that set none of their own, or turn it off.

    Parameters
    ----------
    name
        Environment name.
    body
        The days, or `null` for off.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the environment.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404.
    lakana_core.errors.ValidationError
        Mapped to 422: days out of range, or a protected environment.
    """
    env = catalog.set_environment_retention(principal, name, body.days)
    return EnvironmentOut.model_validate(env)


@router.get("/{name}", response_model=EnvironmentOut)
def get_environment(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Fetch an environment by name.

    Parameters
    ----------
    name
        Environment name.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    return EnvironmentOut.model_validate(catalog.get_environment(principal, name))


@router.get("", response_model=list[EnvironmentOut])
def list_environments(
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> list[EnvironmentOut]:
    """List every environment.

    Filtered to the environments the caller may read — see
    `CatalogService.list_environments`.

    Parameters
    ----------
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    list of EnvironmentOut
        Every environment the caller may read.
    """
    return [EnvironmentOut.model_validate(env) for env in catalog.list_environments(principal)]


@router.delete("/{name}", status_code=204)
def delete_environment(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> None:
    """Delete an environment.

    Parameters
    ----------
    name
        Environment name.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.ConflictError
        A repository is still deployed; mapped to 409.
    """
    catalog.delete_environment(principal, name)
