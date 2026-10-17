"""The secrets resource of the platform API.

Secrets are always addressed through their environment
(`/api/v1/environments/{name}/secrets`), not as a standalone top-level
resource — a secret has no meaning outside the environment it is scoped
to.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.errors import NotFoundError
from lakana_core.models import Secret, SecretUseKind
from lakana_core.secrets.service import SecretService
from lakana_core.secrets.uses import resolve_use
from lakana_server.api.platform.schemas import (
    SecretBind,
    SecretCreate,
    SecretOut,
    SecretRotate,
)
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal, get_secret_service, get_session

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{environment_name}/secrets",
    tags=["secrets"],
)


def _to_out(secret: Secret, environment_name: str, target: str | None) -> SecretOut:
    """Build a response body from a Secret row.

    Parameters
    ----------
    secret
        The row to convert.
    environment_name
        The environment this secret belongs to, since `Secret` itself
        only carries `environment_id`.
    target
        The bound index's or repository's name, from `SecretService.use_target_name`.

    Returns
    -------
    SecretOut
        The response body. Structurally has no field for the plaintext
        or ciphertext.
    """
    return SecretOut(
        id=secret.id,
        name=secret.name,
        environment=environment_name,
        created_by=secret.created_by,
        created_at=secret.created_at,
        format_version=secret.format_version,
        key_id=secret.key_id,
        use=secret.use_kind.value if secret.use_kind is not None else None,
        use_target=target,
    )


def _find_by_name(secrets: list[Secret], name: str) -> Secret:
    """Find a secret by name within an already-fetched list.

    Parameters
    ----------
    secrets
        Secrets already fetched for one environment.
    name
        The name to look for.

    Returns
    -------
    Secret
        The matching secret.

    Raises
    ------
    lakana_core.errors.NotFoundError
        If no secret in `secrets` has that name.
    """
    for secret in secrets:
        if secret.name == name:
            return secret
    raise NotFoundError(f"No secret named {name!r}.")


@router.post("", response_model=SecretOut, status_code=201)
def create_secret(
    environment_name: str,
    body: SecretCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: SecretService = Depends(get_secret_service),
) -> SecretOut:
    """Create a secret.

    Parameters
    ----------
    environment_name
        The environment this secret is scoped to.
    body
        The secret to create.
    principal
        The authenticated caller.
    service
        The secret service for this request.

    Returns
    -------
    SecretOut
        The created secret's metadata.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.AlreadyExistsError
        Mapped to 409.
    lakana_core.errors.LakanaError
        Unmapped, so defaults to 500 — raised if no secrets master key is
        configured. That is a deployment-misconfiguration condition, not
        invalid caller input, so it deliberately is not a `ValidationError`
        (422); see `lakana_core.secrets.keys.EnvKeyProvider`.
    """
    created = service.create(
        principal, environment=environment_name, name=body.name, values=body.values
    )
    return _to_out(created, environment_name, service.use_target_name(created))


@router.get("", response_model=list[SecretOut])
def list_secrets(
    environment_name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: SecretService = Depends(get_secret_service),
) -> list[SecretOut]:
    """List an environment's active secrets.

    Parameters
    ----------
    environment_name
        The environment whose secrets to list.
    principal
        The authenticated caller.
    service
        The secret service for this request.

    Returns
    -------
    list of SecretOut
        Every active secret in this environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    return [
        _to_out(secret, environment_name, service.use_target_name(secret))
        for secret in service.list_for_environment(principal, environment_name)
    ]


@router.patch("/{secret_name}", response_model=SecretOut)
def rotate_secret(
    environment_name: str,
    secret_name: str,
    body: SecretRotate,
    principal: PrincipalRef = Depends(get_principal),
    service: SecretService = Depends(get_secret_service),
) -> SecretOut:
    """Rotate a secret's value.

    Parameters
    ----------
    environment_name
        The environment the secret is scoped to.
    secret_name
        The secret's name.
    body
        The new plaintext values.
    principal
        The authenticated caller.
    service
        The secret service for this request.

    Returns
    -------
    SecretOut
        The rotated secret's metadata.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment or the secret name doesn't
        exist.
    lakana_core.errors.LakanaError
        Unmapped, so defaults to 500 — raised if no secrets master key is
        configured, the same deployment-misconfiguration condition
        `create_secret` documents.
    """
    existing = _find_by_name(service.list_for_environment(principal, environment_name), secret_name)
    rotated = service.rotate(principal, existing.id, values=body.values)
    return _to_out(rotated, environment_name, service.use_target_name(rotated))


@router.put("/{secret_name}/use", response_model=SecretOut)
def bind_secret(
    environment_name: str,
    secret_name: str,
    body: SecretBind,
    principal: PrincipalRef = Depends(get_principal),
    service: SecretService = Depends(get_secret_service),
    session: Session = Depends(get_session),
) -> SecretOut:
    """Bind a secret to a package index, a repository or its environment.

    Parameters
    ----------
    environment_name
        The environment the secret is scoped to.
    secret_name
        The secret's name.
    body
        The use.
    principal
        The authenticated caller.
    service
        The secret service for this request.
    session
        The request's database session, to resolve the target's name.

    Returns
    -------
    SecretOut
        The bound secret's metadata.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404: the environment, the secret or the named target does not exist.
    lakana_core.errors.ValidationError
        Mapped to 422: a name for the environment use, or none for an index or repository.
    lakana_core.errors.PermissionDenied
        Mapped to 403: the caller may not manage secrets in this environment.
    """
    existing = _find_by_name(service.list_for_environment(principal, environment_name), secret_name)
    use = resolve_use(session, SecretUseKind(body.kind), body.target)
    bound = service.bind(principal, existing.id, use)
    return _to_out(bound, environment_name, service.use_target_name(bound))


@router.delete("/{secret_name}", status_code=204)
def revoke_secret(
    environment_name: str,
    secret_name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: SecretService = Depends(get_secret_service),
) -> None:
    """Revoke a secret.

    Parameters
    ----------
    environment_name
        The environment the secret is scoped to.
    secret_name
        The secret's name.
    principal
        The authenticated caller.
    service
        The secret service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment or the secret name doesn't
        exist.
    """
    existing = _find_by_name(service.list_for_environment(principal, environment_name), secret_name)
    service.revoke(principal, existing.id)
