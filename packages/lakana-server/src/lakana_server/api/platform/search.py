"""The search route of the platform API: tables, columns, descriptions, runs and principals."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.search import CatalogSearch, SearchKind
from lakana_server.api.platform.schemas import SearchOut
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_catalog_search, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/environments/{env}", tags=["search"]
)


@router.get("/search", response_model=SearchOut)
def search(
    env: str,
    q: str = Query(description="The text to find; 1 to 200 characters, case-insensitive."),
    kind: SearchKind | None = Query(default=None, description="Only this kind of hit."),
    limit: int = Query(default=20, description="Hits per page, 1 to 100."),
    offset: int = Query(default=0, description="Hits to skip."),
    principal: PrincipalRef = Depends(get_principal),
    service: CatalogSearch = Depends(get_catalog_search),
) -> SearchOut:
    """Search an environment, showing the caller only what they may read.

    Parameters
    ----------
    env
        Environment name.
    q
        The text to find in names and descriptions, in run ids (four characters or more), jobs
        and repositories, and in principal names. Trimmed, 1 to 200 characters.
    kind
        Only this kind of hit.
    limit
        Hits per page, 1 to 100.
    offset
        Hits to skip.
    principal
        The authenticated caller.
    service
        The search service for this request.

    Returns
    -------
    SearchOut
        The page. A table and its columns appear only with `READ_TABLE` on the table, runs only
        with `READ_ENVIRONMENT`, principals only for a caller who may list them; what the caller
        may not see is left out, not refused.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422 by `install_exception_handlers`.
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    page = service.search(principal, env, q, kind=kind, limit=limit, offset=offset)
    return SearchOut.model_validate(page)
