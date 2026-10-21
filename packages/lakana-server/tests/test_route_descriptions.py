"""Tests for the user-facing description of a route and for the documented error answers."""

import pytest
from fastapi import APIRouter, FastAPI, Query

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
