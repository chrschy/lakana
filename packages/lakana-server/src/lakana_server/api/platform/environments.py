"""The environments resource of the platform API."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.orphaned_locations import DEFAULT_OLDER_THAN_DAYS
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_server.api.platform.schemas import (
    EnvironmentCreate,
    EnvironmentOut,
    EnvironmentPatch,
    EnvironmentRetentionIn,
    EnvironmentStorageUpdate,
    EnvironmentUsageOut,
    OrphanedLocationsDeleteIn,
    OrphanedLocationsOut,
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
    deleted: bool = False,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> list[EnvironmentOut]:
    """List every environment, or every deleted one.

    Filtered to the environments the caller may read — see
    `CatalogService.list_environments`.

    Parameters
    ----------
    deleted
        `true` lists the deleted environments whose files are not deleted yet, oldest
        deletion first, each with its `purge_after`, instead.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    list of EnvironmentOut
        Every environment the caller may read.
    """
    found = (
        catalog.list_deleted_environments(principal)
        if deleted
        else catalog.list_environments(principal)
    )
    return [EnvironmentOut.model_validate(env) for env in found]


@router.delete("/{name}", response_model=EnvironmentOut)
def delete_environment(
    name: str,
    purge: bool = False,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Delete an environment: its tables can be restored until `purge_after`.

    Parameters
    ----------
    name
        Environment name.
    purge
        `true` makes everything due at once: the worker's next clean-up deletes the tables'
        files and the environment cannot be restored. Also accepted for an environment that is
        already deleted.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the environment.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The deleted environment, with `deleted_at` and `purge_after`.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`; also for an environment already deleted
        without `purge`.
    lakana_core.errors.ConflictError
        A repository is still deployed; mapped to 409.
    lakana_core.errors.ValidationError
        The environment is protected; mapped to 422.
    """
    return EnvironmentOut.model_validate(catalog.delete_environment(principal, name, purge=purge))


@router.post("/{name}/restore", response_model=EnvironmentOut)
def restore_environment(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Restore a deleted environment within its undo period, with the tables it dropped.

    Parameters
    ----------
    name
        Environment name.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the environment.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The restored environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        No environment of that name; mapped to 404.
    lakana_core.errors.ConflictError
        It is not deleted, its undo period has ended, or the clean-up has started deleting its
        files; mapped to 409.
    """
    return EnvironmentOut.model_validate(catalog.restore_environment(principal, name))


@router.get("/{name}/usage", response_model=EnvironmentUsageOut)
def environment_usage(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentUsageOut:
    """Report how much storage an environment uses; reads the catalog database only.

    Parameters
    ----------
    name
        Environment name.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the environment.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentUsageOut
        Its tables, dropped tables, artifacts and code archives, as the worker's housekeeping
        last measured them.

    Raises
    ------
    lakana_core.errors.NotFoundError
        No environment of that name; mapped to 404.
    """
    return EnvironmentUsageOut.model_validate(catalog.environment_usage(principal, name))


@router.get("/{name}/orphaned-locations", response_model=OrphanedLocationsOut)
def list_orphaned_locations(
    name: str,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> OrphanedLocationsOut:
    """List the table directories older releases left without a location record; delete nothing.

    Parameters
    ----------
    name
        Environment name; a deleted environment is accepted.
    older_than_days
        A directory with a file newer than this many days (1 to 3,650) is listed as recent.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the instance.
    catalog
        The catalog service for this request.

    Returns
    -------
    OrphanedLocationsOut
        The directories found within the time limit.

    Raises
    ------
    lakana_core.errors.NotFoundError
        No environment of that name; mapped to 404.
    lakana_core.errors.ValidationError
        `older_than_days` out of range; mapped to 422.
    """
    found = catalog.find_orphaned_locations(principal, name, older_than_days=older_than_days)
    return OrphanedLocationsOut.model_validate(found)


@router.post("/{name}/orphaned-locations/delete", response_model=OrphanedLocationsOut)
def delete_orphaned_locations(
    name: str,
    body: OrphanedLocationsDeleteIn,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> OrphanedLocationsOut:
    """Delete the table directories without a location record whose files are old enough.

    Parameters
    ----------
    name
        Environment name; a deleted environment is accepted.
    body
        How old the newest file of a directory must be.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the instance.
    catalog
        The catalog service for this request.

    Returns
    -------
    OrphanedLocationsOut
        Each directory, deleted or with the reason it was kept; `complete` is false when the
        time limit stopped the call, and the next call continues.

    Raises
    ------
    lakana_core.errors.NotFoundError
        No environment of that name; mapped to 404.
    lakana_core.errors.ValidationError
        `older_than_days` out of range; mapped to 422.
    """
    report = catalog.delete_orphaned_locations(
        principal, name, older_than_days=body.older_than_days
    )
    return OrphanedLocationsOut.model_validate(report)
