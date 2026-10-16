"""The grants resource of the platform API.

`namespace` crosses this boundary dot-joined (`gold.sales`), converted to
and from the unit-separator storage form here — matching
`namespaces.py`'s existing convention for the same underlying value.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef, SecurableKind
from lakana_core.grants.admin import ineffective_note
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath
from lakana_core.models import Grant
from lakana_server.api.platform.schemas import GrantCreate, GrantOut
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_grant_service, get_principal

router = APIRouter(route_class=AuthenticateFirstRoute, prefix="/api/v1/grants", tags=["grants"])


def _dotted_to_joined(namespace: str | None) -> str | None:
    """Convert a dot-joined namespace path to its unit-separator storage form.

    Parameters
    ----------
    namespace
        A dotted path such as ``"gold.sales"``, or `None`.

    Returns
    -------
    str or None
        The unit-separator-joined storage form, or `None` unchanged.
    """
    if namespace is None:
        return None
    return NamespacePath.from_parts(namespace.split(".")).joined()


def _joined_to_dotted(namespace: str | None) -> str | None:
    """Convert a stored unit-separator namespace path to its dotted form.

    Parameters
    ----------
    namespace
        The unit-separator-joined storage form, or `None`.

    Returns
    -------
    str or None
        The dot-joined display form, or `None` unchanged.
    """
    if namespace is None:
        return None
    return ".".join(NamespacePath.parse(namespace).parts)


def _to_out(grant: Grant) -> GrantOut:
    """Build a response body from a Grant row.

    Parameters
    ----------
    grant
        The row to convert.

    Returns
    -------
    GrantOut
        The response body, with `namespace` converted to its dotted form.
    """
    return GrantOut(
        id=grant.id,
        principal_id=grant.principal_id,
        group_id=grant.group_id,
        role=grant.role,
        action=grant.action,
        kind=grant.kind,
        environment=grant.environment,
        namespace=_joined_to_dotted(grant.namespace),
        table=grant.table,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
        note=ineffective_note(grant.action),
    )


@router.post("", response_model=GrantOut, status_code=201)
def create_grant(
    body: GrantCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: GrantService = Depends(get_grant_service),
) -> GrantOut:
    """Create a grant.

    Parameters
    ----------
    body
        The grant to create.
    principal
        The authenticated caller.
    service
        The grant service for this request.

    Returns
    -------
    GrantOut
        The created grant.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422 by `install_exception_handlers`.
    lakana_core.errors.NotFoundError
        Mapped to 404.
    """
    created = service.grant(
        principal,
        principal=body.principal,
        group=body.group,
        role=body.role,
        action=body.action,
        kind=body.kind,
        environment=body.environment,
        namespace=_dotted_to_joined(body.namespace),
        table=body.table,
    )
    return _to_out(created)


@router.get("", response_model=list[GrantOut])
def list_grants(
    principal_name: str | None = None,
    group_name: str | None = None,
    environment: str | None = None,
    namespace: str | None = None,
    table: str | None = None,
    principal: PrincipalRef = Depends(get_principal),
    service: GrantService = Depends(get_grant_service),
) -> list[GrantOut]:
    """List grants for a principal, a group, or a specific securable.

    Exactly one of `principal_name`/`group_name`/a securable filter should
    be given. When neither `principal_name` nor `group_name` is given, the
    securable's kind is inferred from which of `environment`/`namespace`/
    `table` are given: `table` set means TABLE, else `namespace` set means
    NAMESPACE, else `environment` set means ENVIRONMENT, else INSTANCE.

    Parameters
    ----------
    principal_name
        List this principal's own direct grants.
    group_name
        List this group's own direct grants.
    environment
        The environment to match, for a securable-scoped listing.
    namespace
        The dot-joined namespace to match, for a securable-scoped listing.
    table
        The table to match, for a securable-scoped listing.
    principal
        The authenticated caller.
    service
        The grant service for this request.

    Returns
    -------
    list of GrantOut
        The matching grants.
    """
    if principal_name is not None:
        rows = service.list_for_principal(principal, principal_name)
    elif group_name is not None:
        rows = service.list_for_group(principal, group_name)
    else:
        if table is not None:
            kind = SecurableKind.TABLE
        elif namespace is not None:
            kind = SecurableKind.NAMESPACE
        elif environment is not None:
            kind = SecurableKind.ENVIRONMENT
        else:
            kind = SecurableKind.INSTANCE
        rows = service.list_for_securable(
            principal,
            kind=kind,
            environment=environment,
            namespace=_dotted_to_joined(namespace),
            table=table,
        )
    return [_to_out(row) for row in rows]


@router.delete("/{grant_id}", status_code=204)
def revoke_grant(
    grant_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: GrantService = Depends(get_grant_service),
) -> None:
    """Revoke a grant.

    Parameters
    ----------
    grant_id
        The grant's id.
    principal
        The authenticated caller.
    service
        The grant service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.ConflictError
        Mapped to 409 — revoking this grant would leave zero active
        instance admins.
    """
    service.revoke(principal, grant_id)
