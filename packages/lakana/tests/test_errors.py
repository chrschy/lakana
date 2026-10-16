"""Tests for HTTP status -> SDK exception mapping."""

import httpx
import pytest

from lakana.errors import (
    AuthenticationError,
    Conflict,
    LakanaError,
    NotFound,
    PayloadTooLarge,
    PermissionDenied,
    ServerError,
    ValidationError,
    raise_for_response,
)


def _response(status: int, *, json: object = None, text: str | None = None) -> httpx.Response:
    """Build an `httpx.Response` detached from any real request.

    Parameters
    ----------
    status
        The HTTP status code.
    json
        A JSON-serializable response body.
    text
        A raw text response body, for a non-JSON body.

    Returns
    -------
    httpx.Response
        The constructed response.
    """
    return httpx.Response(status, request=httpx.Request("GET", "http://x/y"), json=json, text=text)


@pytest.mark.parametrize(
    ("status", "expected_cls"),
    [
        (401, AuthenticationError),
        (403, PermissionDenied),
        (404, NotFound),
        (409, Conflict),
        (413, PayloadTooLarge),
        (422, ValidationError),
        (500, ServerError),
    ],
)
def test_status_maps_to_expected_exception(status: int, expected_cls: type[LakanaError]) -> None:
    """Each mapped status raises its dedicated exception, carrying detail/type."""
    response = _response(status, json={"detail": "oh no", "type": "SomeError"})
    with pytest.raises(expected_cls) as excinfo:
        raise_for_response(response)
    exc = excinfo.value
    assert exc.status == status
    assert exc.detail == "oh no"
    assert exc.error_type == "SomeError"


def test_unknown_4xx_status_maps_to_base_error() -> None:
    """A 4xx status with no dedicated mapping raises the base LakanaError."""
    response = _response(418, json={"detail": "teapot", "type": "ImATeapot"})
    with pytest.raises(LakanaError) as excinfo:
        raise_for_response(response)
    assert type(excinfo.value) is LakanaError
    assert excinfo.value.status == 418
    assert excinfo.value.detail == "teapot"


def test_non_json_error_body_still_maps_by_status() -> None:
    """A non-JSON error body still raises the right class, with the text as detail."""
    response = _response(404, text="not json at all")
    with pytest.raises(NotFound) as excinfo:
        raise_for_response(response)
    assert excinfo.value.detail == "not json at all"
    assert excinfo.value.error_type is None


def test_json_body_that_is_not_a_dict_falls_back_to_text() -> None:
    """A JSON body that parses but isn't a dict falls back to the raw text."""
    response = _response(500, json=["oops"])
    with pytest.raises(ServerError) as excinfo:
        raise_for_response(response)
    assert excinfo.value.detail == '["oops"]'
    assert excinfo.value.error_type is None


def test_json_dict_missing_detail_falls_back_to_text() -> None:
    """A JSON dict without a `detail` key falls back to the raw response text."""
    response = _response(409, json={"type": "SomeError"})
    with pytest.raises(Conflict) as excinfo:
        raise_for_response(response)
    assert excinfo.value.detail == '{"type":"SomeError"}'
    assert excinfo.value.error_type == "SomeError"


def test_null_detail_falls_back_to_reason_phrase_not_literal_none() -> None:
    """An explicit `"detail": null` never renders as the literal string "None"."""
    response = _response(404, json={"detail": None, "type": "NotFoundError"})
    with pytest.raises(NotFound) as excinfo:
        raise_for_response(response)
    assert excinfo.value.detail == response.reason_phrase
    assert excinfo.value.detail != "None"
    assert excinfo.value.error_type == "NotFoundError"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_says_so_and_names_the_next_step(status: int) -> None:
    """A 3xx answer has no body: the error must not be an empty message."""
    with pytest.raises(LakanaError) as excinfo:
        raise_for_response(_response(status, text=""))
    assert excinfo.value.status == status
    assert str(excinfo.value) == (
        f"The server answered with a redirect (HTTP {status}); check the profile's URL."
    )


def test_success_response_raises_nothing() -> None:
    """A 2xx response is not mapped to an exception."""
    raise_for_response(_response(200, json={"ok": True}))


def test_config_error_carries_no_status() -> None:
    """A plain LakanaError subclass constructed without status/type has None for both."""
    from lakana.errors import ConfigError

    exc = ConfigError("bad config")
    assert exc.status is None
    assert exc.error_type is None
    assert exc.detail == "bad config"


def test_the_retry_after_header_of_an_error_is_kept_as_the_server_sent_it() -> None:
    """The header is what a caller needs to wait; `None` when the response has none."""
    response = httpx.Response(503, json={"detail": "busy"}, headers={"Retry-After": "5"})
    with pytest.raises(ServerError) as excinfo:
        raise_for_response(response)
    assert excinfo.value.retry_after == "5"
    with pytest.raises(ServerError) as without:
        raise_for_response(_response(503, json={"detail": "busy"}))
    assert without.value.retry_after is None
    assert LakanaError("x").retry_after is None
