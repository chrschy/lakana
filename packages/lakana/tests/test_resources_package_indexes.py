"""The SDK's reading of a package index answer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana.errors import InvalidResponse
from lakana.resources.package_indexes import PackageIndexes

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_ANSWER = {
    "id": "00000000-0000-0000-0000-000000000001",
    "name": "team",
    "url": "https://pkgs.example.org/simple",
    "credential_secret": "team-index",
    "created_by": None,
    "created_at": "2026-10-07T12:00:00+00:00",
    "updated_at": "2026-10-07T12:00:00+00:00",
}


def _indexes(body: Any) -> PackageIndexes:  # ruff: ignore[any-type]
    """Build the resource over a transport that always answers with `body`.

    Parameters
    ----------
    body
        The JSON the fake server sends, or bytes for a body that is not JSON.

    Returns
    -------
    PackageIndexes
        The resource.
    """
    handler: Callable[[httpx.Request], httpx.Response] = (
        (lambda request: httpx.Response(200, content=body))
        if isinstance(body, bytes)
        else (lambda request: httpx.Response(200, json=body))
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return PackageIndexes(HttpTransport(_CONFIG, http_client=client))


def test_an_answer_without_an_optional_field_is_read() -> None:
    answer = dict(_ANSWER)
    del answer["credential_secret"]
    del answer["created_by"]
    index = _indexes(answer).get("team")
    assert (index.credential_secret, index.created_by) == (None, None)
    assert [i.name for i in _indexes([answer]).list()] == ["team"]


def test_an_answer_with_an_unknown_field_is_read() -> None:
    index = _indexes({**_ANSWER, "added_later": 1}).get("team")
    assert index.name == "team"


@pytest.mark.parametrize("field", ["id", "name", "url", "created_at", "updated_at"])
def test_an_answer_without_a_required_field_is_an_invalid_response(field: str) -> None:
    answer = dict(_ANSWER)
    del answer[field]
    with pytest.raises(InvalidResponse, match="not what a Lakana server sends"):
        _indexes(answer).get("team")
    with pytest.raises(InvalidResponse):
        _indexes([answer]).list()


@pytest.mark.parametrize("body", [b"<html>", b'"text"', b"[1]"])
def test_an_answer_that_is_not_an_index_is_an_invalid_response(body: bytes) -> None:
    with pytest.raises(InvalidResponse):
        _indexes(body).get("team")
    with pytest.raises(InvalidResponse):
        _indexes(body).list()
