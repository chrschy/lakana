"""Tests for the headers that announce a deprecated route (RFC 9745 and RFC 8594)."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute
from starlette.types import Message, Receive, Scope, Send

from lakana_server.app import ROUTERS, create_app
from lakana_server.deprecation import DeprecationHeaders, deprecated, deprecation_headers
from lakana_server.settings import Settings

pytestmark = pytest.mark.unit


def _client() -> TestClient:
    """Build a client for an app with a current, a deprecated and a sunset route.

    Returns
    -------
    TestClient
        A client of the app, wrapped in `DeprecationHeaders`.
    """
    app = FastAPI()

    @app.get("/current")
    def current() -> dict[str, str]:
        """Answer from a route that is not deprecated.

        Returns
        -------
        dict of str to str
            A fixed answer.
        """
        return {"ok": "yes"}

    @app.get("/old", **deprecated(since=date(2027, 3, 1)))
    def old() -> dict[str, str]:
        """Answer from a deprecated route without a removal date.

        Returns
        -------
        dict of str to str
            A fixed answer.
        """
        return {"ok": "yes"}

    @app.get("/going", **deprecated(since=date(2027, 3, 1), sunset=date(2027, 9, 1)))
    def going() -> None:
        """Refuse from a deprecated route with a removal date.

        Raises
        ------
        HTTPException
            Always, to show that a refusal carries the headers too.
        """
        raise HTTPException(status_code=409, detail="no")

    app.add_middleware(DeprecationHeaders)
    return TestClient(app)


def test_a_route_that_is_not_deprecated_has_no_deprecation_headers() -> None:
    """Nothing changes for a current route."""
    response = _client().get("/current")
    assert response.status_code == 200
    assert "deprecation" not in response.headers
    assert "sunset" not in response.headers


def test_a_deprecated_route_answers_with_the_date_it_was_deprecated() -> None:
    """``Deprecation`` is a structured-field date: ``@`` and the Unix time (RFC 9745)."""
    response = _client().get("/old")
    assert response.status_code == 200
    assert response.headers["deprecation"] == "@1803859200"
    assert "sunset" not in response.headers


def test_a_route_with_a_removal_date_also_answers_with_sunset_even_when_it_refuses() -> None:
    """``Sunset`` is an HTTP date (RFC 8594), on every answer of the route."""
    response = _client().get("/going")
    assert response.status_code == 409
    assert response.headers["deprecation"] == "@1803859200"
    assert response.headers["sunset"] == "Wed, 01 Sep 2027 00:00:00 GMT"


def test_an_unknown_route_has_no_deprecation_headers() -> None:
    """A 404 belongs to no route."""
    assert "deprecation" not in _client().get("/missing").headers


def test_deprecated_marks_the_route_in_the_openapi_document() -> None:
    """The document carries ``deprecated: true`` and the dates as extensions."""
    paths = _client().app.openapi()["paths"]  # type: ignore[attr-defined]
    assert paths["/going"]["get"]["deprecated"] is True
    assert paths["/going"]["get"]["x-deprecated-since"] == "2027-03-01"
    assert paths["/going"]["get"]["x-sunset"] == "2027-09-01"
    assert "x-sunset" not in paths["/old"]["get"]
    assert "deprecated" not in paths["/current"]["get"]


def test_a_route_marked_deprecated_without_a_date_has_no_headers() -> None:
    """RFC 9745 needs a date; such a route is caught by the test below, not guessed at."""

    class _Route:
        deprecated = True
        openapi_extra = None

    assert deprecation_headers(_Route()) == []


def test_a_scope_that_is_not_http_passes_through_unchanged() -> None:
    """Only HTTP answers get headers; a lifespan or websocket message is passed on as it is."""
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        """Send one lifespan message.

        Parameters
        ----------
        scope
            The scope.
        receive
            Unused.
        send
            Where the message goes.
        """
        await send({"type": "lifespan.startup.complete"})

    async def receive() -> Message:  # ruff: ignore[unused-async] -- the ASGI signature
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:  # ruff: ignore[unused-async] -- the ASGI signature
        sent.append(message)

    asyncio.run(DeprecationHeaders(app)({"type": "lifespan"}, receive, send))
    assert sent == [{"type": "lifespan.startup.complete"}]


def test_the_application_sends_the_deprecation_headers(settings: Settings) -> None:
    """`create_app` installs the middleware.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    """
    assert any(m.cls is DeprecationHeaders for m in create_app(settings).user_middleware)


def _routes() -> list[BaseRoute]:
    """List every route of every router the application includes.

    Returns
    -------
    list of BaseRoute
        The routes.
    """
    return [route for router in ROUTERS for route in router.routes]


def test_every_deprecated_route_says_since_when() -> None:
    """A route is deprecated only with `deprecated(since=...)`, so its answers carry the header."""
    undated = [
        getattr(route, "path", "")
        for route in _routes()
        if getattr(route, "deprecated", False)
        and "x-deprecated-since" not in (getattr(route, "openapi_extra", None) or {})
    ]
    assert undated == []
