"""A bound on the size of a JSON request body, enforced before anything is parsed.

Every route has one of two kinds of body, and each has its own limit:

- **A JSON body** (a pydantic model, or the Iceberg REST request): at most
  `Settings.request_body.max_bytes` (`LAKANA_REQUEST_BODY__MAX_BYTES`, 1 MiB). Checked here,
  before authentication and before FastAPI reads or decodes a byte. Parsing JSON in Python peaks
  at 20 to 30 times the size of the text, so this limit is what bounds that cost.
- **A streamed body**, read piece by piece by the route itself: table data appends and replaces
  (`Settings.append.max_bytes`), artifact files (the sizes in the upload's manifest) and the
  artifact manifest request (`MAX_MANIFEST_REQUEST_BYTES`, 16 MiB). These routes are marked with
  `streams_its_own_body`; the middleware leaves them alone.

One rule decides every request: a declared length over the limit is refused at once; otherwise
the bytes are counted as they arrive and reading stops at the limit. Headers never vouch for a
body: a `Content-Length` beside `Transfer-Encoding: chunked`, a non-numeric length and no header at
all are all judged by the bytes the server actually delivers. A body within the limit is handed to
the application whole and unchanged; a client that disconnects part-way ends the wait, because the
disconnect is passed on to the application as it would be without this middleware.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_Endpoint = TypeVar("_Endpoint", bound=Callable[..., Any])

_MARK = "streams_its_own_body"


def streams_its_own_body(endpoint: _Endpoint) -> _Endpoint:
    """Mark a route function that reads its request body as a stream with a limit of its own.

    Put this below the route decorator (it must mark the function the decorator registers); the
    JSON body limit then does not apply to the route.

    Parameters
    ----------
    endpoint
        The route function.

    Returns
    -------
    Callable
        `endpoint`, marked.
    """
    setattr(endpoint, _MARK, True)
    return endpoint


def streaming_routes(routers: Iterable[APIRouter]) -> list[APIRoute]:
    """List the routes marked with `streams_its_own_body`.

    Parameters
    ----------
    routers
        The routers the application includes, none of them with a prefix added at inclusion.

    Returns
    -------
    list of APIRoute
        Every marked route.
    """
    return [
        route
        for router in routers
        for route in router.routes
        if isinstance(route, APIRoute) and getattr(route.endpoint, _MARK, False)
    ]


def _too_large_message(limit: int) -> str:
    """Write the answer for a body over the limit.

    Parameters
    ----------
    limit
        The limit in bytes.

    Returns
    -------
    str
        A message that names the limit, the setting and what to do next.
    """
    return (
        f"The request body is larger than {limit} bytes, the most the server accepts for a JSON "
        "request. Send less in one request. If this request is legitimate, an operator can raise "
        "the limit with LAKANA_REQUEST_BODY__MAX_BYTES."
    )


class BodyLimitMiddleware:
    """Refuse a JSON request body over a size limit with 413, before it is read or parsed.

    Parameters
    ----------
    app
        The application this wraps.
    max_bytes
        The limit, in bytes; a body of exactly this size is accepted.
    exempt
        The routes that stream their own body (`streaming_routes`).
    iceberg
        The routes of the Iceberg REST catalog, whose errors have their own shape. Matched the
        way `exempt` is, so a `root_path` is handled the same.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        exempt: list[APIRoute],
        iceberg: Iterable[BaseRoute] = (),
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self._exempt = exempt
        self._iceberg = list(iceberg)

    def _is_exempt(self, scope: Scope) -> bool:
        """Say whether the request is for a route that streams its own body.

        Parameters
        ----------
        scope
            The request scope.

        Returns
        -------
        bool
            `True` for a marked route.
        """
        return any(route.matches(scope)[0] == Match.FULL for route in self._exempt)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer 413 in the shape of the surface the request is for.

        Parameters
        ----------
        scope
            The request scope.
        receive
            The ASGI receive channel.
        send
            The ASGI send channel.
        """
        message = _too_large_message(self.max_bytes)
        if any(route.matches(scope)[0] != Match.NONE for route in self._iceberg):
            content: dict[str, Any] = {
                "error": {"message": message, "type": "PayloadTooLargeError", "code": 413}
            }
        else:
            content = {"detail": message, "type": "PayloadTooLargeError"}
        await JSONResponse(content, status_code=413)(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, or refuse it when its body is over the limit.

        Parameters
        ----------
        scope
            The request scope.
        receive
            The ASGI receive channel.
        send
            The ASGI send channel.
        """
        if scope["type"] != "http" or self._is_exempt(scope):
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._refuse(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, _replay([message], receive), send)
                return
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await self._refuse(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        await self.app(
            scope, _replay([{"type": "http.request", "body": bytes(body)}], receive), send
        )


def _replay(messages: list[Message], then: Receive) -> Receive:
    """Build a receive channel that hands back messages already read.

    Parameters
    ----------
    messages
        What to hand back, in order.
    then
        The channel to read from once they are gone; the client's own disconnect arrives there.

    Returns
    -------
    Callable
        The new receive channel.
    """
    pending = list(messages)

    async def receive() -> Message:
        """Return the next held message, then whatever the client sends next.

        Returns
        -------
        dict
            An ASGI message.
        """
        if pending:
            return pending.pop(0)
        return await then()

    return receive
