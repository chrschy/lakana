"""Tests for the Iceberg-router error-shaping route class."""

import logging

import pytest
from fastapi import APIRouter, Depends, FastAPI, Header
from fastapi.testclient import TestClient

from lakana_core.errors import KeptAfterCheckError, NotFoundError, NotPublishedError
from lakana_server.api.iceberg.route import IcebergRoute


def _dependency_that_raises(authorization: str | None = Header(None)) -> str:
    """Raise a LakanaError from a dependency, to prove the route catches it too.

    Parameters
    ----------
    authorization
        Unused; present so this can be wired as a FastAPI dependency.

    Raises
    ------
    NotFoundError
        Always.
    """
    raise NotFoundError("simulated dependency failure")


def test_the_route_reshapes_errors_raised_inside_a_dependency() -> None:
    """FastAPI resolves dependencies inside the handler a custom route wraps.

    This is the property the whole error-shaping design depends on: a
    dependency-raised exception must be caught exactly like one raised in
    the endpoint body, or an external client hitting a bad token would see
    FastAPI's default error body instead of the Iceberg spec's shape.
    """
    router = APIRouter(route_class=IcebergRoute)

    @router.get("/protected")
    def protected(_: str = Depends(_dependency_that_raises)) -> dict:
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/protected")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"message": "simulated dependency failure", "type": "NotFoundError", "code": 404}
    }


def test_the_route_reshapes_errors_raised_in_the_endpoint_body() -> None:
    """The more ordinary case: the endpoint itself raises."""
    from lakana_core.errors import ConflictError

    router = APIRouter(route_class=IcebergRoute)

    @router.get("/conflict")
    def conflict() -> dict:
        raise ConflictError("simulated")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/conflict")

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "ConflictError"


def test_pydantic_validation_errors_are_reshaped_too() -> None:
    """The C1 correction: pydantic's ValidationError, not any LakanaError, from parsing."""
    import pydantic

    router = APIRouter(route_class=IcebergRoute)

    @router.get("/bad-parse")
    def bad_parse() -> dict:
        pydantic.TypeAdapter(int).validate_python("not-an-int")
        return {}

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/bad-parse")

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"


def test_an_unexpected_exception_still_gets_the_spec_shaped_body() -> None:
    """The terminal catch-all the design's mapping table mandates.

    The table's last row reads "uncaught `LakanaError` / anything else | 500",
    and *anything else* is the load-bearing half: a bare `KeyError`,
    `AttributeError` or third-party exception must not fall through to
    Starlette's plain-text default on a surface whose error body is
    externally specified. The message is deliberately generic — an internal
    exception's own text may carry implementation detail.
    """
    router = APIRouter(route_class=IcebergRoute)

    @router.get("/boom")
    def boom() -> dict:
        raise RuntimeError("a secret internal detail")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/boom")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {"message": "Internal server error", "type": "RuntimeError", "code": 500}
    }


def test_a_starlette_http_exception_keeps_its_own_status_and_detail() -> None:
    """FastAPI's own request-handling machinery, not just endpoint code, can raise.

    `fastapi.routing.get_request_handler` raises a plain
    `starlette.exceptions.HTTPException(400, "There was an error parsing the
    body")` when a declared body can't even be read as JSON — for example a
    body that isn't valid UTF-8. That happens inside the same call the
    terminal `except Exception` wraps, so before this fix it was misreported
    as a spec-shaped 500 with `type: "HTTPException"` instead of the 400
    FastAPI itself would answer with on a plain route. The route must give
    this exception its own branch, ordered before the terminal catch-all,
    that echoes `exc.status_code` and `exc.detail` rather than hardcoding 500.
    """
    router = APIRouter(route_class=IcebergRoute)

    @router.post("/echo")
    def echo(payload: dict) -> dict:
        return payload

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/echo",
        content=b"\xff\xfe not valid utf8",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "There was an error parsing the body",
            "type": "HTTPException",
            "code": 400,
        }
    }


def test_an_unmodeled_exception_is_logged_before_the_opaque_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The terminal catch-all must not swallow an unmodeled error silently.

    Before this fix, an exception outside the caught vocabulary propagated
    to `ServerErrorMiddleware`, which logs a traceback an operator could
    correlate with the client-visible `type`. Reshaping it into a client
    response inside this route means that traceback never reaches anywhere
    else, so the route itself must log it — matching `_OPAQUE_MESSAGE`'s own
    docstring, which justifies hiding the message by pointing at "the
    server-side traceback" that this test proves still gets written.
    """
    router = APIRouter(route_class=IcebergRoute)

    @router.get("/boom-quietly")
    def boom_quietly() -> dict:
        raise RuntimeError("a secret internal detail")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    # lakana_core.migrations.env calls logging.config.fileConfig, whose default
    # disable_existing_loggers=True marks every logger that already existed
    # process-wide (this module's included) as disabled — regardless of
    # level. When the full suite runs test_migrations.py before this test,
    # that flag alone silences caplog even though the level is right, so it
    # is saved and forced off for the duration of this test.
    route_logger = logging.getLogger("lakana_server.api.iceberg.route")
    previously_disabled = route_logger.disabled
    route_logger.disabled = False
    try:
        with caplog.at_level(logging.ERROR, logger="lakana_server.api.iceberg.route"):
            response = client.get("/boom-quietly")
    finally:
        route_logger.disabled = previously_disabled

    assert response.status_code == 500
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert "RuntimeError" in caplog.text
    assert record.exc_info is not None


def test_not_published_is_a_404_with_the_reason_in_the_iceberg_error_shape() -> None:
    """The load answers 404 so that an engine reads "no such table", with the text kept."""
    router = APIRouter(route_class=IcebergRoute)

    @router.get("/unpublished")
    def unpublished() -> dict:
        raise NotPublishedError("`gold.sales` has not been published.")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/unpublished")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "message": "`gold.sales` has not been published.",
            "type": "NotPublishedError",
            "code": 404,
        }
    }


def test_kept_after_check_is_a_404_with_the_reason_in_the_iceberg_error_shape() -> None:
    """A task's load of a kept table answers as the not-published load does, with its own text."""
    router = APIRouter(route_class=IcebergRoute)

    @router.get("/kept")
    def kept() -> dict:
        raise KeptAfterCheckError("`gold.sales` was kept after a failed check.")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/kept")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "message": "`gold.sales` was kept after a failed check.",
            "type": "KeptAfterCheckError",
            "code": 404,
        }
    }


def test_an_unreachable_storage_is_a_503_with_the_message() -> None:
    """An engine reads 503 as "try again"; the message names the environment, not the storage."""
    from lakana_core.errors import StorageUnavailableError
    from lakana_core.storage.unavailable import TABLE_STORAGE_UNREACHABLE

    router = APIRouter(route_class=IcebergRoute)
    message = TABLE_STORAGE_UNREACHABLE.format(environment="dev")

    @router.get("/table")
    def table() -> dict:
        raise StorageUnavailableError(message)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/table")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "message": message,
        "type": "StorageUnavailableError",
        "code": 503,
    }
