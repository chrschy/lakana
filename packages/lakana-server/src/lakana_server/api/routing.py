"""The route class every JSON route of the server uses: authenticate before the body is read."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from lakana_server.deps import authenticate_before_body, get_principal


def depends_on_get_principal(dependant: Dependant) -> bool:
    """Say whether a route authenticates through `get_principal`, at any depth.

    Parameters
    ----------
    dependant
        The route's dependency tree.

    Returns
    -------
    bool
        Whether `get_principal` is in it.
    """
    return any(
        child.call is get_principal or depends_on_get_principal(child)
        for child in dependant.dependencies
    )


_SECTION = re.compile(
    r"^(?:Parameters|Other Parameters|Returns|Yields|Receives|Raises|Warns|Warnings|See Also"
    r"|Notes|References|Examples)\n-+$",
    re.MULTILINE,
)
"""A numpydoc section heading: a known section name over a line of dashes."""


def split_public_text(doc: str | None) -> tuple[str | None, str | None]:
    """Split a route's docstring into the summary and description its API users read.

    The text before the first numpydoc section (``Parameters``, ``Returns``, ``Raises``, ...)
    is written for the people who call the route; the sections are for the people who
    maintain it and stay out of the OpenAPI document.

    Parameters
    ----------
    doc
        The docstring of a route's function, or ``None``.

    Returns
    -------
    tuple of (str or None, str or None)
        The first line without its closing full stop, and the rest of the public text, each
        ``None`` when there is none.
    """
    if doc is None or not doc.strip():
        return None, None
    heading = _SECTION.search(doc)
    public = (doc[: heading.start()] if heading else doc).strip()
    summary, _, description = public.partition("\n")
    description = re.sub(r"(?<!\n)\n(?!\n)", " ", description.strip())
    return summary.strip().rstrip("."), description or None


class AuthenticateFirstRoute(APIRoute):
    """An `APIRoute` that verifies the caller before FastAPI reads a JSON body.

    FastAPI reads and decodes a request body before it resolves any dependency, so an
    unauthenticated caller could make the server decode a body (and get a 422 where a 401 is
    right). A route with no body model, or one that does not authenticate with `get_principal`, is
    unchanged.

    The route's summary and description in the OpenAPI document come from the public part of
    its docstring (`split_public_text`) unless the decorator sets them, so the docstring stays
    the one place a route is described.
    """

    # APIRoute's own keyword arguments, passed on whole, so there is no narrower type.
    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:  # ruff: ignore[any-type]
        """Build the route, taking its summary and description from the docstring.

        Parameters
        ----------
        path
            The route's path.
        endpoint
            The function that serves it.
        **kwargs
            Every other `APIRoute` argument. A ``summary`` or ``description`` given here is kept.
        """
        summary, description = split_public_text(inspect.getdoc(endpoint))
        if kwargs.get("summary") is None:
            kwargs["summary"] = summary
        if kwargs.get("description") is None:
            kwargs["description"] = description
        super().__init__(path, endpoint, **kwargs)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the handler of a route with a body model in an authentication step.

        Returns
        -------
        Callable
            The original handler, or one that calls `authenticate_before_body` first when the
            route has a JSON body and authenticates with `get_principal`. An authentication
            failure is raised from inside the handler, so a subclass that shapes errors (the
            Iceberg route) shapes this one too.
        """
        original = super().get_route_handler()
        if self.body_field is None or not depends_on_get_principal(self.dependant):
            return original

        async def authenticated_first(request: Request) -> Response:
            """Authenticate, then run the original handler.

            Parameters
            ----------
            request
                The incoming request.

            Returns
            -------
            Response
                The original handler's response.
            """
            await run_in_threadpool(authenticate_before_body, request)
            return await original(request)

        return authenticated_first
