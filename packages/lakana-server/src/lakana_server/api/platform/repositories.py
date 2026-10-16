"""The repositories resource of the platform API.

Instance-wide: a repository is registered once by an admin and named by
deployers in every environment.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.models import Repository, RepositoryKind
from lakana_core.projects.repositories import RepositoryService
from lakana_server.api.platform.schemas import RepositoryCreate, RepositoryOut, RepositoryUpdate
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal, get_repository_service

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/repositories", tags=["repositories"]
)


def _to_out(repo: Repository) -> RepositoryOut:
    """Build a response body.

    Parameters
    ----------
    repo
        The row.

    Returns
    -------
    RepositoryOut
        The body.
    """
    return RepositoryOut(
        id=repo.id,
        name=repo.name,
        kind=repo.kind.value,
        url=repo.url,
        credential_secret=repo.credential_secret,
        credential_username=repo.credential_username,
        ssh_known_hosts=repo.ssh_known_hosts,
        default_ref=repo.default_ref,
        created_by=repo.created_by,
        created_at=repo.created_at,
        updated_at=repo.updated_at,
    )


@router.post("", response_model=RepositoryOut, status_code=201)
def create_repository(
    body: RepositoryCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
) -> RepositoryOut:
    """Register a repository.

    Parameters
    ----------
    body
        The repository.
    principal
        The authenticated caller.
    service
        The repository service.

    Returns
    -------
    RepositoryOut
        The registered repository.
    """
    repo = service.create(
        principal,
        name=body.name,
        kind=RepositoryKind(body.kind),
        url=body.url,
        credential_secret=body.credential_secret,
        credential_username=body.credential_username,
        ssh_known_hosts=body.ssh_known_hosts,
        default_ref=body.default_ref,
    )
    return _to_out(repo)


@router.get("", response_model=list[RepositoryOut])
def list_repositories(
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
) -> list[RepositoryOut]:
    """List repositories.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The repository service.

    Returns
    -------
    list of RepositoryOut
        Sorted by name.
    """
    return [_to_out(repo) for repo in service.list_repositories(principal)]


@router.get("/{name}", response_model=RepositoryOut)
def get_repository(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
) -> RepositoryOut:
    """Show a repository.

    Parameters
    ----------
    name
        Its name.
    principal
        The authenticated caller.
    service
        The repository service.

    Returns
    -------
    RepositoryOut
        The repository.
    """
    return _to_out(service.get(principal, name))


@router.patch("/{name}", response_model=RepositoryOut)
def update_repository(
    name: str,
    body: RepositoryUpdate,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
) -> RepositoryOut:
    """Change a repository's URL, credential, username, known hosts or default ref.

    Parameters
    ----------
    name
        Its name.
    body
        Only the fields present change; `null` clears.
    principal
        The authenticated caller.
    service
        The repository service.

    Returns
    -------
    RepositoryOut
        The updated repository.
    """
    return _to_out(service.update(principal, name, body.model_dump(exclude_unset=True)))


@router.delete("/{name}", status_code=204)
def delete_repository(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
) -> None:
    """Delete a repository; refused (409) while any environment deploys it.

    Undeploy it first: `DELETE /environments/{env}/deployments/{repo}` (`lakana undeploy`).

    Parameters
    ----------
    name
        Its name.
    principal
        The authenticated caller.
    service
        The repository service.
    """
    service.delete(principal, name)
