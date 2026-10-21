"""How a deprecated route of the platform REST API announces itself.

A route is deprecated with `deprecated`, which marks it in the OpenAPI document
(`deprecated: true`, and the dates as `x-deprecated-since` and `x-sunset`):

    @router.get("/old", **deprecated(since=date(2027, 3, 1), sunset=date(2027, 9, 1)))

`DeprecationHeaders` then adds to every answer of that route, a refusal (4xx) included, the
`Deprecation` header of RFC 9745 (`@` and the Unix time of the day it was deprecated) and, when a
removal date is known, the `Sunset` header of RFC 8594 (an HTTP date). A 500 from an
unexpected error does not carry them: Starlette's outermost error handler writes it, outside
every middleware an application adds. The policy that decides
when a route may be deprecated and removed is the user documentation's "The REST API as a
contract" page.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from email.utils import format_datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SINCE = "x-deprecated-since"
_SUNSET = "x-sunset"


def deprecated(*, since: date, sunset: date | None = None) -> dict[str, Any]:
    """Build the route arguments that deprecate a route.

    Parameters
    ----------
    since
        The day the route was deprecated (the release that deprecated it).
    sunset
        The day it may be removed, when known.

    Returns
    -------
    dict of str to Any
        `deprecated` and `openapi_extra`, to pass to the route decorator with `**`.
    """
    extra = {_SINCE: since.isoformat()}
    if sunset is not None:
        extra[_SUNSET] = sunset.isoformat()
    return {"deprecated": True, "openapi_extra": extra}


def _midnight(day: str) -> datetime:
    """Turn an ISO date into midnight UTC of that day.

    Parameters
    ----------
    day
        A date such as `2027-03-01`.

    Returns
    -------
    datetime
        Midnight UTC of the day.
    """
    return datetime.combine(date.fromisoformat(day), time(), UTC)


def deprecation_headers(route: object) -> list[tuple[bytes, bytes]]:
    """Build the headers a route's answers carry.

    Parameters
    ----------
    route
        The route that answered (the scope's `route`), or `None` when none matched.

    Returns
    -------
    list of tuple of bytes
        `Deprecation` and, when the route has a removal date, `Sunset`; empty for a route that
        is not deprecated with `deprecated`.
    """
    extra = getattr(route, "openapi_extra", None) or {}
    if not getattr(route, "deprecated", False) or _SINCE not in extra:
        return []
    headers = [(b"deprecation", f"@{int(_midnight(extra[_SINCE]).timestamp())}".encode())]
    if _SUNSET in extra:
        headers.append(
            (b"sunset", format_datetime(_midnight(extra[_SUNSET]), usegmt=True).encode())
        )
    return headers


class DeprecationHeaders:
    """Add the deprecation headers to the answers of a deprecated route (not to an unexpected 500).

    Parameters
    ----------
    app
        The application to wrap.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, adding the headers when the answer starts.

        Parameters
        ----------
        scope
            The request scope; the router stores the matched route in it.
        receive
            The request's receive channel.
        send
            The response's send channel.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            """Add the headers to the start of the answer.

            Parameters
            ----------
            message
                An ASGI message of the answer.
            """
            if message["type"] == "http.response.start":
                headers = deprecation_headers(scope.get("route"))
                if headers:
                    message = {**message, "headers": [*message.get("headers", ()), *headers]}
            await send(message)

        await self.app(scope, receive, send_with_headers)
