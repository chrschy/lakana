"""Tests for HttpTransport: request construction, retries, and error mapping."""

import math
from collections.abc import Callable, Iterator
from typing import cast

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport, query_params
from lakana._routes import Route
from lakana.errors import InvalidResponse, LakanaConnectionError, NotFound, ValidationError

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")


def _transport_with_transport(handler: Callable[[httpx.Request], httpx.Response]) -> HttpTransport:
    """Build an `HttpTransport` whose own `httpx.Client` uses a mock transport.

    Parameters
    ----------
    handler
        The mock transport's request handler.

    Returns
    -------
    HttpTransport
        A transport backed by `httpx.MockTransport(handler)`.
    """
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return HttpTransport(_CONFIG, http_client=client)


def test_get_retries_on_connect_error_and_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GET failing twice with ConnectError succeeds on the third attempt."""
    sleeps: list[float] = []
    monkeypatch.setattr("lakana._http.time.sleep", sleeps.append)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    transport = _transport_with_transport(handler)
    response = transport.request(Route("GET", "/x"))
    assert response.status_code == 200
    assert attempts["n"] == 3
    assert sleeps == [0.2, 0.4]


def test_get_fails_after_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GET failing on every attempt surfaces as LakanaConnectionError."""
    monkeypatch.setattr("lakana._http.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    transport = _transport_with_transport(handler)
    with pytest.raises(LakanaConnectionError) as excinfo:
        transport.request(Route("GET", "/x"))
    assert "supersecret" not in str(excinfo.value)


def test_post_is_not_retried_on_connect_error() -> None:
    """A POST failing with ConnectError is not retried and surfaces immediately."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    transport = _transport_with_transport(handler)
    with pytest.raises(LakanaConnectionError):
        transport.request(Route("POST", "/x"))
    assert attempts["n"] == 1


def test_path_parameters_are_quoted() -> None:
    """A path parameter containing a space is percent-quoted in the URL."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode()
        return httpx.Response(200, json={})

    transport = _transport_with_transport(handler)
    transport.request(Route("GET", "/things/{name}"), path_params={"name": "a b"})
    assert seen["path"] == "/things/a%20b"


def test_authorization_header_is_present() -> None:
    """Every request carries the configured bearer token."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={})

    transport = _transport_with_transport(handler)
    transport.request(Route("GET", "/x"))
    assert seen["auth"] == "Bearer lakana_pat_abc_supersecret"


def test_extra_headers_are_merged_with_authorization() -> None:
    """Caller-supplied headers are sent alongside the Authorization header."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["content-type"] = request.headers["content-type"]
        return httpx.Response(200, json={})

    transport = _transport_with_transport(handler)
    transport.request(
        Route("POST", "/x"), headers={"Content-Type": "application/vnd.apache.arrow.stream"}
    )
    assert seen["auth"] == "Bearer lakana_pat_abc_supersecret"
    assert seen["content-type"] == "application/vnd.apache.arrow.stream"


def test_error_response_is_mapped() -> None:
    """A non-2xx response is mapped to the matching SDK exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "gone", "type": "NotFoundError"})

    transport = _transport_with_transport(handler)
    with pytest.raises(NotFound):
        transport.request(Route("GET", "/x"))


def test_token_never_appears_in_a_connection_error_message() -> None:
    """`LakanaConnectionError`'s message names only the URL and exception type."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    transport = _transport_with_transport(handler)
    with pytest.raises(LakanaConnectionError) as excinfo:
        transport.request(Route("POST", "/x"))
    message = str(excinfo.value)
    assert "supersecret" not in message
    assert "api.example" in message
    assert "ConnectError" in message


def test_transport_uses_paths_relative_to_an_externally_supplied_client() -> None:
    """Requests use paths relative to a caller-supplied client's own base_url.

    This is the shape other tests rely on: Starlette's `TestClient` has
    base_url `http://testserver`, unrelated to `config.url`.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")
    config = ResolvedConfig(url="http://ignored.example", token="lakana_pat_a_s")
    transport = HttpTransport(config, http_client=client)
    transport.request(Route("GET", "/x"))
    assert seen["url"] == "http://testserver/x"


def test_transport_builds_its_own_client_with_configured_base_url() -> None:
    """Without an external client, the transport builds one using config.url."""
    transport = HttpTransport(_CONFIG, timeout=5.0)
    try:
        assert str(transport._client.base_url) == "http://api.example"
        connect_timeout = transport._client.timeout.connect
        assert connect_timeout is not None
        assert math.isclose(connect_timeout, 5.0)
    finally:
        transport.close()


def test_close_closes_a_client_it_built_itself() -> None:
    """`close()` closes the httpx.Client this transport built for itself."""
    transport = HttpTransport(_CONFIG)
    transport.close()
    assert transport._client.is_closed


def test_close_does_not_close_a_caller_supplied_client() -> None:
    """`close()` never closes an httpx.Client the transport did not build."""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    transport = HttpTransport(_CONFIG, http_client=client)
    transport.close()
    assert not client.is_closed
    client.close()


@pytest.mark.parametrize("value", ["", ".", "..", "/", "a/b"])
def test_forbidden_path_parameter_values_are_refused(value: str) -> None:
    """Values that could smuggle a dot-segment or extra path segment are refused."""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={})

    transport = _transport_with_transport(handler)
    with pytest.raises(ValidationError):
        transport.request(Route("GET", "/things/{name}"), path_params={"name": value})
    assert called["n"] == 0


# --- Per-request timeout override ------------------


class _RecordingClient:
    """A minimal stand-in for `httpx.Client`, recording each call's kwargs.

    Parameters
    ----------
    calls
        The list this instance appends each `request()` call's kwargs to.
    """

    def __init__(self, calls: list[dict[str, object]]) -> None:
        self._calls = calls

    def request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        """Record `kwargs` and return a bare 200 response.

        Parameters
        ----------
        method
            The HTTP method.
        path
            The request path.
        **kwargs
            Everything `HttpTransport` passed through, recorded verbatim.

        Returns
        -------
        httpx.Response
            An empty, successful response.
        """
        self._calls.append(kwargs)
        return httpx.Response(
            200, json={"ok": True}, request=httpx.Request(method, "http://x" + path)
        )


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("timeout", [None, 12.5])
def test_request_forwards_a_timeout_override_only_when_given(
    method: str, timeout: float | None
) -> None:
    """`request(timeout=...)` reaches the underlying client only when explicitly given.

    Passing `timeout=None` to `httpx.Client.request` itself would mean "no
    timeout at all" (infinite), not "use the client's own default" -- so
    the kwarg must be omitted entirely when no override is given, covering
    both the GET (`_send_with_retry`) and non-GET (`_send_once`) paths.
    """
    calls: list[dict[str, object]] = []
    transport = HttpTransport(_CONFIG, http_client=cast(httpx.Client, _RecordingClient(calls)))

    transport.request(Route(method, "/x"), timeout=timeout)

    if timeout is None:
        assert "timeout" not in calls[0]
    else:
        assert calls[0]["timeout"] == timeout


def test_query_params_drops_unset_values_but_keeps_falsy_ones() -> None:
    """`query_params` omits `None` (httpx would send it as ``""``) and keeps `0`/`False`/``""``."""
    assert query_params(a=None, b=0, c=False, d="", e="x") == {
        "b": 0,
        "c": False,
        "d": "",
        "e": "x",
    }


def test_stream_yields_the_body_and_raises_for_an_error_with_its_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={"detail": "no such file", "type": "NotFoundError"})
        assert request.headers["Authorization"].startswith("Bearer ")
        assert request.url.params["x"] == "1"
        return httpx.Response(200, content=b"abcdef")

    transport = _transport_with_transport(handler)
    route = Route("GET", "/files/{name}")
    with transport.stream(
        route, path_params={"name": "a"}, params={"x": "1"}, timeout=5.0
    ) as chunks:
        assert b"".join(chunks) == b"abcdef"
    with (
        pytest.raises(NotFound, match="no such file"),
        transport.stream(route, path_params={"name": "missing"}),
    ):
        raise AssertionError("the body of an error must not be offered")

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with (
        pytest.raises(LakanaConnectionError, match="ConnectError"),
        _transport_with_transport(broken).stream(route, path_params={"name": "a"}),
    ):
        raise AssertionError("unreachable")


def test_a_body_that_breaks_while_it_is_read_is_a_connection_error_not_a_short_body() -> None:
    class Cut(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"abc"
            raise httpx.RemoteProtocolError("peer closed the connection before the body ended")

    transport = _transport_with_transport(lambda request: httpx.Response(200, stream=Cut()))
    seen: list[bytes] = []
    with (
        pytest.raises(LakanaConnectionError, match="RemoteProtocolError"),
        transport.stream(Route("GET", "/x")) as chunks,
    ):
        seen.extend(chunks)
    assert seen == [b"abc"]


def test_stream_refuses_an_unsafe_path_parameter() -> None:
    transport = _transport_with_transport(lambda request: httpx.Response(200))
    with (
        pytest.raises(ValidationError),
        transport.stream(Route("GET", "/x/{a}"), path_params={"a": ".."}),
    ):
        raise AssertionError("unreachable")


def _gzip_lie(request: httpx.Request) -> httpx.Response:
    """Answer with a body that says it is gzip and is not.

    Parameters
    ----------
    request
        The request.

    Returns
    -------
    httpx.Response
        The answer.
    """
    return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"this is not gzip")


def test_a_body_that_cannot_be_decoded_is_an_invalid_response_not_a_raw_httpx_error() -> None:
    transport = _transport_with_transport(_gzip_lie)
    with (
        pytest.raises(InvalidResponse) as caught,
        transport.stream(Route("GET", "/x")) as chunks,
    ):
        list(chunks)
    text = str(caught.value)
    assert "could not be decoded" in text
    assert "http://api.example" in text
    assert "Check the URL" in text
    assert "supersecret" not in text


def test_an_answer_that_cannot_be_decoded_is_an_invalid_response_for_every_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lakana._http.time.sleep", lambda _seconds: None)
    transport = _transport_with_transport(_gzip_lie)
    for route in (Route("GET", "/x"), Route("POST", "/x")):
        with pytest.raises(InvalidResponse, match="could not be decoded"):
            transport.request(route)
    with pytest.raises(InvalidResponse, match="could not be decoded"):
        transport.request(Route("GET", "/x"), retry=False)
