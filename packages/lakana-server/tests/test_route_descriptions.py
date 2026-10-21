"""Tests for the user-facing description of a route and for the documented error answers."""

import importlib
import inspect
import pkgutil
from typing import Annotated

import pytest
from fastapi import APIRouter, FastAPI, Path, Query, params
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from lakana_server.api.responses import ERROR_MEANING, ErrorOut, errors
from lakana_server.api.routing import (
    AuthenticateFirstRoute,
    parameter_docs,
    split_public_text,
)


def test_the_public_text_ends_at_the_first_numpydoc_section() -> None:
    """Everything before ``Parameters`` is the public text; the first line is the summary."""
    doc = (
        "Do the thing.\n\nIt does it well.\n\nIt is quick.\n\n"
        "Parameters\n----------\nx\n    The x.\n\nRaises\n------\nFoo\n    Never.\n"
    )
    assert split_public_text(doc) == ("Do the thing", "It does it well.\n\nIt is quick.")


@pytest.mark.parametrize("heading", ["Returns", "Raises", "Notes", "See Also", "Examples"])
def test_any_numpydoc_section_ends_the_public_text(heading: str) -> None:
    """A docstring without ``Parameters`` is cut at whichever section comes first."""
    doc = f"Do it.\n\nMore.\n\n{heading}\n{'-' * len(heading)}\nstuff\n"
    assert split_public_text(doc) == ("Do it", "More.")


def test_a_docstring_with_one_line_has_no_description() -> None:
    """A summary alone leaves the description empty."""
    assert split_public_text("Do it.\n\nParameters\n----------\nx\n    The x.\n") == (
        "Do it",
        None,
    )


def test_a_dash_line_in_prose_is_not_a_section() -> None:
    """A heading needs a line of dashes right under a known section name."""
    assert split_public_text("Do it.\n\nWord\n----\nmore text\n") == (
        "Do it",
        "Word ---- more text",
    )


def test_no_docstring_gives_nothing() -> None:
    """A function without a docstring gets FastAPI's default summary."""
    assert split_public_text(None) == (None, None)
    assert split_public_text("   ") == (None, None)


def _app_with(router: APIRouter) -> dict:
    """Serve one router and return the OpenAPI document.

    Parameters
    ----------
    router
        The router to include.

    Returns
    -------
    dict
        The OpenAPI document.
    """
    app = FastAPI()
    app.include_router(router)
    return app.openapi()


def test_a_route_takes_summary_and_description_from_its_docstring() -> None:
    """The document shows the public text and none of the maintainer text."""
    router = APIRouter(route_class=AuthenticateFirstRoute)

    @router.get("/x")
    def route() -> None:
        """Show the thing.

        Anyone may ask.

        Raises
        ------
        lakana_core.errors.NotFoundError
            Mapped to 404.
        """

    operation = _app_with(router)["paths"]["/x"]["get"]
    assert operation["summary"] == "Show the thing"
    assert operation["description"] == "Anyone may ask."


def test_an_explicit_summary_and_description_win() -> None:
    """What the decorator says is not replaced by the docstring."""
    router = APIRouter(route_class=AuthenticateFirstRoute)

    @router.get("/x", summary="Mine", description="Mine too.")
    def route() -> None:
        """Show the thing.

        Anyone may ask.
        """

    operation = _app_with(router)["paths"]["/x"]["get"]
    assert (operation["summary"], operation["description"]) == ("Mine", "Mine too.")


def test_errors_document_each_status_once_with_the_error_body() -> None:
    """Each code has a description and the ``detail``/``type`` body of the platform API."""
    router = APIRouter(route_class=AuthenticateFirstRoute)

    @router.get("/x", responses=errors(404, 409))
    def route() -> None:
        """Show the thing."""

    responses = _app_with(router)["paths"]["/x"]["get"]["responses"]
    assert set(responses) == {"200", "404", "409"}
    assert responses["404"]["description"] == ERROR_MEANING[404]
    schema = responses["409"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ErrorOut"}
    assert set(ErrorOut.model_fields) == {"detail", "type"}


def test_an_unknown_status_is_refused() -> None:
    """A code with no shared meaning is a mistake in the route, found at import."""
    with pytest.raises(KeyError):
        errors(418)


def test_parameter_docs_read_the_parameters_section() -> None:
    """Each parameter's text is its indented lines joined, with or without a type."""
    doc = (
        "Do it.\n\nParameters\n----------\nenv\n    Environment name.\n"
        "limit : int\n    How many,\n    at most.\n\nReturns\n-------\nint\n    A number.\n"
    )
    assert parameter_docs(doc) == {"env": "Environment name.", "limit": "How many, at most."}
    assert parameter_docs(None) == {}
    assert parameter_docs("Do it.") == {}


def test_an_indented_line_before_any_parameter_name_is_ignored() -> None:
    """A continuation line with no parameter above it belongs to none."""
    doc = "Do it.\n\nParameters\n----------\n    stray text\nenv\n    Environment name.\n"
    assert parameter_docs(doc) == {"env": "Environment name."}


def test_a_request_parameter_takes_its_description_from_the_docstring() -> None:
    """Path and query parameters show the text of the ``Parameters`` section; a set one wins."""
    router = APIRouter(route_class=AuthenticateFirstRoute)

    @router.get("/x/{name}")
    def route(name: str, days: int = 7, own: str = Query(default="", description="Mine.")) -> None:
        """Show the thing.

        Parameters
        ----------
        name
            The thing's name.
        days
            How far back to look.
        own
            Not used.
        """

    parameters = _app_with(router)["paths"]["/x/{name}"]["get"]["parameters"]
    assert {p["name"]: p["description"] for p in parameters} == {
        "name": "The thing's name.",
        "days": "How far back to look.",
        "own": "Mine.",
    }


def test_a_route_with_a_one_line_text_publishes_that_line_only() -> None:
    """Without a description FastAPI would fall back to the whole docstring; here it does not."""
    router = APIRouter(route_class=AuthenticateFirstRoute)

    @router.get("/x")
    def route() -> None:
        """Show the thing.

        Raises
        ------
        lakana_core.errors.NotFoundError
            Mapped to 404.
        """

    operation = _app_with(router)["paths"]["/x"]["get"]
    assert operation["summary"] == "Show the thing"
    assert operation["description"] == "Show the thing."


def test_describing_a_parameter_keeps_the_constraints_declared_in_annotated() -> None:
    """A length or range set inside ``Annotated`` still rejects a value that breaks it."""
    router = APIRouter(route_class=AuthenticateFirstRoute)
    job = Annotated[str, Path(min_length=1, max_length=5)]
    count = Annotated[int, Query(ge=1, le=10)]

    @router.get("/x/{name}")
    def route(name: job, n: count = 3) -> None:
        """Show the thing.

        Parameters
        ----------
        name
            The thing's name.
        n
            How many.
        """

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/x/abcde").status_code == 200
    assert client.get("/x/abcdef").status_code == 422
    assert client.get("/x/abc?n=11").status_code == 422
    assert client.get("/x/abc?n=0").status_code == 422
    described = {
        p["name"]: p["description"]
        for p in app.openapi()["paths"]["/x/{name}"]["get"]["parameters"]
    }
    assert described == {"name": "The thing's name.", "n": "How many."}


def _platform_endpoints() -> list:
    """List the function of every route of the platform routers.

    Returns
    -------
    list
        The endpoints.
    """
    from lakana_server.app import ROUTERS

    return [r.endpoint for router in ROUTERS for r in router.routes if isinstance(r, APIRoute)]


def _parameters_of(document: dict) -> dict:
    """Collect every parameter of a document without its description.

    Parameters
    ----------
    document
        An OpenAPI document.

    Returns
    -------
    dict
        The parameters by method, path and name, each without ``description``.
    """

    def bare(value: object) -> object:
        """Drop every ``description`` key, however deep.

        Parameters
        ----------
        value
            A JSON value.

        Returns
        -------
        object
            The value without descriptions.
        """
        if isinstance(value, dict):
            return {k: bare(v) for k, v in value.items() if k != "description"}
        if isinstance(value, list):
            return [bare(v) for v in value]
        return value

    return {
        (method, path, p["in"], p["name"]): bare(p)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        for p in operation.get("parameters", [])
    }


def test_describing_parameters_changes_nothing_but_their_descriptions() -> None:
    """Every parameter schema of the platform API is the same with and without the texts."""
    from lakana_server.app import ROUTERS

    def document() -> dict:
        app = FastAPI()
        for router in ROUTERS:
            app.include_router(router)
        return app.openapi()

    with_texts = document()
    endpoints = _platform_endpoints()
    saved = {id(e): e.__dict__.pop("__signature__", None) for e in endpoints}
    try:
        without = document()
    finally:
        for endpoint in endpoints:
            if saved[id(endpoint)] is not None:
                endpoint.__signature__ = saved[id(endpoint)]
    assert _parameters_of(with_texts) == _parameters_of(without)
    assert any(saved.values())


def test_every_platform_function_with_a_dependency_is_a_route() -> None:
    """A route function that lost its decorator would silently leave the API."""
    import lakana_server.api.platform as package

    routes = set(_platform_endpoints())
    orphans = []
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for name, function in inspect.getmembers(module, inspect.isfunction):
            if function.__module__ != module.__name__:
                continue
            depends = [
                p
                for p in inspect.signature(function).parameters.values()
                if isinstance(p.default, params.Depends)
            ]
            if depends and function not in routes:
                orphans.append(f"{info.name}.{name}")
    assert orphans == []
