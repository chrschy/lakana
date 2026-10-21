"""The route class every JSON route of the server uses: authenticate before the body is read."""

from __future__ import annotations

import copy
import inspect
import operator
import re
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Path, Query, Request, Response
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic.fields import FieldInfo
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


def parameter_docs(doc: str | None) -> dict[str, str]:
    """Read the ``Parameters`` section of a docstring.

    Parameters
    ----------
    doc
        The docstring of a route's function, or ``None``.

    Returns
    -------
    dict of str to str
        The text of each documented parameter, its lines joined by a space, by name.
    """
    if doc is None:
        return {}
    found: dict[str, str] = {}
    in_section = False
    name = None
    lines = doc.split("\n")
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if _SECTION.match(f"{line}\n{following}"):
            in_section = line.strip() == "Parameters"
            name = None
        elif in_section and line.strip() and set(line.strip()) != {"-"}:
            if line.startswith((" ", "\t")):
                if name is not None:
                    found[name] = f"{found[name]} {line.strip()}".strip()
            else:
                name = line.split(":")[0].strip()
                found[name] = ""
    return found


def describe_parameters(
    endpoint: Callable[..., Any], dependant: Dependant, docs: dict[str, str]
) -> None:
    """Give the path and query parameters of a function the text its docstring documents.

    FastAPI builds a route's parameters again from the function's signature when the router is
    included, so the text is written into the signature (``__signature__``) rather than into
    the route's own parameter objects.

    Parameters
    ----------
    endpoint
        The route's function; its ``__signature__`` is replaced.
    dependant
        The route's dependency tree, which says which parameters are path and query parameters.
    docs
        The documented text of each parameter, by name (`parameter_docs`).
    """
    signature = inspect.signature(endpoint)
    changed: dict[str, inspect.Parameter] = {}
    for field in (*dependant.path_params, *dependant.query_params):
        param = signature.parameters[field.name]
        info = field.field_info
        if info.description is not None or field.name not in docs:
            continue
        if isinstance(param.default, FieldInfo):
            described = copy.copy(param.default)
            described.description = docs[field.name]
            changed[field.name] = param.replace(default=described)
        else:
            kind = Path if field in dependant.path_params else Query
            annotated = operator.getitem(
                Annotated, (info.annotation, kind(description=docs[field.name]))
            )
            changed[field.name] = param.replace(annotation=annotated)
    if changed:
        endpoint.__signature__ = signature.replace(  # ty: ignore[unresolved-attribute]
            parameters=[changed.get(name, param) for name, param in signature.parameters.items()]
        )


class AuthenticateFirstRoute(APIRoute):
    """An `APIRoute` that verifies the caller before FastAPI reads a JSON body.

    FastAPI reads and decodes a request body before it resolves any dependency, so an
    unauthenticated caller could make the server decode a body (and get a 422 where a 401 is
    right). A route with no body model, or one that does not authenticate with `get_principal`, is
    unchanged.

    The route's summary and description in the OpenAPI document come from the public part of
    its docstring (`split_public_text`) unless the decorator sets them, and the description of
    each path and query parameter from the docstring's ``Parameters`` section
    (`parameter_docs`), so the docstring stays the one place a route is described.
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
        if not kwargs.get("description"):
            # Never None or empty: FastAPI would fall back to the whole docstring.
            kwargs["description"] = description or (f"{summary}." if summary else None)
        super().__init__(path, endpoint, **kwargs)
        describe_parameters(endpoint, self.dependant, parameter_docs(inspect.getdoc(endpoint)))

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
