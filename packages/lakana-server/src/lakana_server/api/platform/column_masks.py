"""The column-masks resource of the platform API.

`namespace` crosses this boundary dot-joined (`gold.sales`), converted to
and from the unit-separator storage form here — matching `grants.py`'s
existing convention for the same underlying value.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from lakana_core.auth import PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath
from lakana_core.models import ColumnMask
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_server.api.platform.schemas import ColumnMaskCreate, ColumnMaskOut
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import (
    check_statement,
    get_column_mask_service,
    get_principal,
)

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/column-masks", tags=["column-masks"]
)


def _dotted_to_joined(namespace: str) -> str:
    """Convert a dot-joined namespace path to its unit-separator storage form.

    Parameters
    ----------
    namespace
        A dotted path such as ``"gold.sales"``.

    Returns
    -------
    str
        The unit-separator-joined storage form.
    """
    return NamespacePath.from_parts(namespace.split(".")).joined()


def _joined_to_dotted(namespace: str) -> str:
    """Convert a stored unit-separator namespace path to its dotted form.

    Parameters
    ----------
    namespace
        The unit-separator-joined storage form.

    Returns
    -------
    str
        The dot-joined display form.
    """
    return ".".join(NamespacePath.parse(namespace).parts)


def _to_out(row: ColumnMask) -> ColumnMaskOut:
    """Build a response body from a ColumnMask row.

    Parameters
    ----------
    row
        The row to convert.

    Returns
    -------
    ColumnMaskOut
        The response body, with `namespace` converted to its dotted form.
    """
    return ColumnMaskOut(
        id=row.id,
        principal_id=row.principal_id,
        group_id=row.group_id,
        environment=row.environment,
        namespace=_joined_to_dotted(row.namespace),
        table=row.table,
        column=row.column,
        expression=row.expression,
        created_by=row.created_by,
        created_at=row.created_at,
    )


@router.post("", response_model=ColumnMaskOut, status_code=201)
def create_column_mask(
    body: ColumnMaskCreate,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    service: ColumnMaskService = Depends(get_column_mask_service),
) -> ColumnMaskOut:
    """Create a column mask.

    Parameters
    ----------
    body
        The column mask to create.
    request
        The request, used to reach the statement length limit.
    principal
        The authenticated caller.
    service
        The column mask service for this request.

    Returns
    -------
    ColumnMaskOut
        The created column mask.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422 by `install_exception_handlers`, including for a column the
        table does not have and for an expression longer than
        `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`.
    lakana_core.errors.NotFoundError
        Mapped to 404.
    """
    check_statement(request, body.expression, "The column-mask expression")
    created = service.create(
        principal,
        principal=body.principal,
        group=body.group,
        environment=body.environment,
        namespace=_dotted_to_joined(body.namespace),
        table=body.table,
        column=body.column,
        expression=body.expression,
    )
    return _to_out(created)


@router.get("", response_model=list[ColumnMaskOut])
def list_column_masks(
    principal_name: str | None = None,
    group_name: str | None = None,
    environment: str | None = None,
    namespace: str | None = None,
    table: str | None = None,
    principal: PrincipalRef = Depends(get_principal),
    service: ColumnMaskService = Depends(get_column_mask_service),
) -> list[ColumnMaskOut]:
    """List column masks for a principal, a group, or a specific table.

    Exactly one of `principal_name`/`group_name`/(`environment`+`namespace`+
    `table` together) must be given — unlike grants, there is no
    instance-wide default, since a column mask is always table-scoped.

    Parameters
    ----------
    principal_name
        List this principal's own direct masks.
    group_name
        List this group's own direct masks.
    environment
        The environment to match, for a table-scoped listing.
    namespace
        The dot-joined namespace to match, for a table-scoped listing.
    table
        The table to match, for a table-scoped listing.
    principal
        The authenticated caller.
    service
        The column mask service for this request.

    Returns
    -------
    list of ColumnMaskOut
        The matching column masks.

    Raises
    ------
    lakana_core.errors.ValidationError
        If none of the valid parameter combinations is given.
    """
    if principal_name is not None:
        rows = service.list_for_principal(principal, principal_name)
    elif group_name is not None:
        rows = service.list_for_group(principal, group_name)
    elif environment is not None and namespace is not None and table is not None:
        rows = service.list_for_table(
            principal, environment=environment, namespace=_dotted_to_joined(namespace), table=table
        )
    else:
        raise ValidationError(
            "Provide 'principal_name', 'group_name', or 'environment'+'namespace'+'table' together."
        )
    return [_to_out(row) for row in rows]


@router.delete("/{column_mask_id}", status_code=204)
def revoke_column_mask(
    column_mask_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: ColumnMaskService = Depends(get_column_mask_service),
) -> None:
    """Revoke a column mask.

    Parameters
    ----------
    column_mask_id
        The column mask's id.
    principal
        The authenticated caller.
    service
        The column mask service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.revoke(principal, column_mask_id)
