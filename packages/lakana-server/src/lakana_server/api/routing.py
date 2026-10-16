"""The route class every JSON route of the server uses: authenticate before the body is read."""

from __future__ import annotations

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


class AuthenticateFirstRoute(APIRoute):
    """An `APIRoute` that verifies the caller before FastAPI reads a JSON body.

    FastAPI reads and decodes a request body before it resolves any dependency, so an
    unauthenticated caller could make the server decode a body (and get a 422 where a 401 is
    right). A route with no body model, or one that does not authenticate with `get_principal`, is
    unchanged.
    """

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
