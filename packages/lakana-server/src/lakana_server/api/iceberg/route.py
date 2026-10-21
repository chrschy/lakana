"""The error-shaping route class for the Iceberg REST router.

FastAPI's exception handlers are registered on the application as a whole,
but the Iceberg REST specification mandates a response body
(`{"error": {"message", "type", "code"}}`) different from the platform API's.
This route class wraps each endpoint's dependency resolution and handler
call, catching the same exception vocabulary and reshaping it before it
would ever reach the app-level handlers `install_exception_handlers` sets up
— those serve the platform API only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

import pydantic
from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from lakana_core.errors import (
    AlreadyExistsError,
    AuthenticationError,
    ConflictError,
    KeptAfterCheckError,
    LakanaError,
    NotFoundError,
    NotPublishedError,
    PermissionDenied,
    StorageUnavailableError,
    ValidationError,
)
from lakana_server.api.routing import AuthenticateFirstRoute

logger = logging.getLogger(__name__)
"""Logger for exceptions outside the caught vocabulary.

The terminal `except Exception` branch reshapes an unmodeled exception into
an opaque client response and must not let it propagate further, so this is
the only remaining way its traceback reaches the server side to be
correlated with the client-visible `type` (see `_OPAQUE_MESSAGE`).
"""


_STATUS_CODES: dict[type[Exception], int] = {
    AuthenticationError: 401,
    NotFoundError: 404,
    NotPublishedError: 404,
    KeptAfterCheckError: 404,
    AlreadyExistsError: 409,
    ConflictError: 409,
    ValidationError: 422,
    PermissionDenied: 403,
    StorageUnavailableError: 503,
    pydantic.ValidationError: 422,
    RequestValidationError: 422,
}
"""Maps each caught exception type to its HTTP status code.

`NotPublishedError` is a 404 here, not the 409 of the platform API: an engine reads the
Iceberg REST catalog's 404 as "no such table", which is what a table with no published
snapshot is to it, and the message says why. `KeptAfterCheckError` (a task refused a table
that was kept after a failed check) is a 404 for the same reason: to the task's engine
the table is not there to read. `StorageUnavailableError` (the environment's storage did not
answer) is a 503 with `Retry-After: 5`, which an engine may try again. The lookup is by
exact type, so a subclass of a mapped error needs its own entry.

`pydantic.ValidationError` is listed explicitly and is distinct from
`lakana_core.errors.ValidationError`, which is also in this table under its
own name — two different classes named `ValidationError` (a third,
`pyiceberg.exceptions.ValidationError`, is unrelated and not caught here).
`RequestValidationError` is a fourth, separate class: FastAPI raises it when
a request body fails to parse against its Pydantic model, before an
endpoint's own body ever runs — distinct from a `pydantic.ValidationError`
raised inside a handler doing its own parsing (for example `iceberg_json`).
"""


_OPAQUE_MESSAGE = "Internal server error"
"""Stands in for the message of an exception outside the caught vocabulary.

An error Lakana did not model is by definition one whose text was never
written for an external reader: it may name an internal path, a SQL
fragment, or a third-party library's internals. The exception's class name
is still reported in `type`, which is enough to correlate a client-side
report with the server-side traceback without spilling the message itself.
"""


def _error_response(exc: Exception, status_code: int, message: str) -> JSONResponse:
    """Build the Iceberg REST specification's error body.

    Parameters
    ----------
    exc
        The caught exception, whose class name becomes the body's `type`.
    status_code
        The HTTP status to answer with, echoed into the body's `code` as
        the specification requires.
    message
        The human-readable message for the body.

    Returns
    -------
    JSONResponse
        A response carrying `{"error": {"message", "type", "code"}}`; for a storage that did not
        answer also `Retry-After: 5`, as the platform API sends.
    """
    headers = {"Retry-After": "5"} if isinstance(exc, StorageUnavailableError) else None
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": type(exc).__name__, "code": status_code}},
        headers=headers,
    )


def _error_message(exc: Exception) -> str:
    """Render a caught exception's message for the Iceberg error body.

    Parameters
    ----------
    exc
        The caught exception.

    Returns
    -------
    str
        For a `RequestValidationError`, each error's dotted location and
        message joined together — `str(exc)` on this class defaults to a
        vague ``"N validation errors for Request"`` with no detail on what
        was actually wrong. For every other caught exception, `str(exc)`.
    """
    if isinstance(exc, RequestValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
    return str(exc)


class IcebergRoute(AuthenticateFirstRoute):
    """An APIRoute that reshapes errors into the Iceberg REST error body.

    Used as `route_class` for the Iceberg router only (`router.py`). The
    platform API keeps FastAPI's default route class and relies on
    `install_exception_handlers` instead.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the original handler with Iceberg-shaped error handling.

        Returns
        -------
        Callable
            A handler that catches `LakanaError`, `pydantic.ValidationError`,
            and `RequestValidationError` — raised either inside a dependency
            or inside the endpoint body, since both run inside the call this
            wraps — and returns the spec-mandated JSON shape instead of
            letting them propagate to FastAPI's default handling. A
            `starlette.exceptions.HTTPException` raised by FastAPI's own
            request-handling machinery (for example an invalid-UTF-8 body)
            is reshaped into the same JSON body but keeps its own
            `status_code` and `detail` rather than being folded into the
            terminal 500 — it is a genuine client error, not an unmodeled
            one. A terminal `except Exception` catches everything else as a
            500 in the same shape, which is what the design's mapping
            table means by its last row, "uncaught `LakanaError` / anything
            else". Without it a stray `KeyError` or third-party exception
            reaches Starlette's default handler and the client gets a
            plain-text body on the one surface whose error shape is
            externally specified. That branch also logs the exception,
            traceback included, via `logger.exception` before responding —
            otherwise nothing outside this process would ever record that
            an unmodeled error happened, and `_OPAQUE_MESSAGE`'s promise
            that the client-visible `type` can be correlated with a
            server-side traceback would have no traceback to correlate
            with. `KeyboardInterrupt` and `SystemExit` derive from
            `BaseException`, not `Exception`, and so still propagate.
        """
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            """Call the original handler, reshaping any caught error.

            Parameters
            ----------
            request
                The incoming request.

            Returns
            -------
            Response
                The original response on success, or a spec-shaped
                `JSONResponse` on a caught error.
            """
            try:
                return await original_route_handler(request)
            except (LakanaError, pydantic.ValidationError, RequestValidationError) as exc:
                return _error_response(exc, _STATUS_CODES.get(type(exc), 500), _error_message(exc))
            except HTTPException as exc:
                return _error_response(exc, exc.status_code, str(exc.detail))
            except Exception as exc:
                logger.exception("Unhandled exception in Iceberg REST route: %s", exc)
                return _error_response(exc, 500, _OPAQUE_MESSAGE)

        return custom_route_handler
