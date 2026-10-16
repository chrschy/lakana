"""The row-filters resource of the platform API.

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
from lakana_core.models import RowFilter
from lakana_core.policy.row_filters import RowFilterService
from lakana_server.api.platform.schemas import RowFilterCreate, RowFilterOut
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import check_statement, get_principal, get_row_filter_service

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/row-filters", tags=["row-filters"]
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


def _to_out(row: RowFilter) -> RowFilterOut:
    """Build a response body from a RowFilter row.

    Parameters
    ----------
    row
        The row to convert.

    Returns
    -------
    RowFilterOut
        The response body, with `namespace` converted to its dotted form.
    """
    return RowFilterOut(
        id=row.id,
        principal_id=row.principal_id,
        group_id=row.group_id,
        environment=row.environment,
        namespace=_joined_to_dotted(row.namespace),
        table=row.table,
        expression=row.expression,
        created_by=row.created_by,
        created_at=row.created_at,
    )


@router.post("", response_model=RowFilterOut, status_code=201)
def create_row_filter(
    body: RowFilterCreate,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    service: RowFilterService = Depends(get_row_filter_service),
) -> RowFilterOut:
    """Create a row filter.

    Parameters
    ----------
    body
        The row filter to create.
    request
        The request, used to reach the statement length limit.
    principal
        The authenticated caller.
    service
        The row filter service for this request.

    Returns
    -------
    RowFilterOut
        The created row filter.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422 by `install_exception_handlers`, including for an expression longer than
        `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`.
    lakana_core.errors.NotFoundError
        Mapped to 404.
    """
    check_statement(request, body.expression, "The row-filter expression")
    created = service.create(
        principal,
        principal=body.principal,
        group=body.group,
        environment=body.environment,
        namespace=_dotted_to_joined(body.namespace),
        table=body.table,
        expression=body.expression,
    )
    return _to_out(created)


@router.get("", response_model=list[RowFilterOut])
def list_row_filters(
    principal_name: str | None = None,
    group_name: str | None = None,
    environment: str | None = None,
    namespace: str | None = None,
    table: str | None = None,
    principal: PrincipalRef = Depends(get_principal),
    service: RowFilterService = Depends(get_row_filter_service),
) -> list[RowFilterOut]:
    """List row filters for a principal, a group, or a specific table.

    Exactly one of `principal_name`/`group_name`/(`environment`+`namespace`+
    `table` together) must be given — unlike grants, there is no
    instance-wide default, since a row filter is always table-scoped.

    Parameters
    ----------
    principal_name
        List this principal's own direct filters.
    group_name
        List this group's own direct filters.
    environment
        The environment to match, for a table-scoped listing.
    namespace
        The dot-joined namespace to match, for a table-scoped listing.
    table
        The table to match, for a table-scoped listing.
    principal
        The authenticated caller.
    service
        The row filter service for this request.

    Returns
    -------
    list of RowFilterOut
        The matching row filters.

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


@router.delete("/{row_filter_id}", status_code=204)
def revoke_row_filter(
    row_filter_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: RowFilterService = Depends(get_row_filter_service),
) -> None:
    """Revoke a row filter.

    Parameters
    ----------
    row_filter_id
        The row filter's id.
    principal
        The authenticated caller.
    service
        The row filter service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.revoke(principal, row_filter_id)
