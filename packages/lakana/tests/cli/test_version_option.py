"""Tests for `lakana --version`: the CLI's version and, when reachable, the instance's."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest
from conftest import BASE_URL, FakeApi, invoke, make_state

import lakana
from lakana.cli._state import CliState
from lakana.errors import ConfigError, LakanaConnectionError


def test_version_prints_the_cli_and_the_instance_version(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/info", json={"version": "9.8.7"})

    result = invoke(make_state(tmp_path, api), "--version")

    assert result.exit_code == 0, result.stderr
    assert f"lakana {lakana.__version__}" in result.stdout
    assert f"instance {BASE_URL}: 9.8.7" in result.stdout
    assert [r.url.path for r in api.requests] == ["/api/v1/info"]


def test_version_as_json(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/info", json={"version": "9.8.7"})

    result = invoke(make_state(tmp_path, api), "--output", "json", "--version")

    assert json.loads(result.stdout) == {
        "cli": lakana.__version__,
        "instance": {"url": BASE_URL, "version": "9.8.7", "error": None},
    }


def test_version_asks_with_a_short_timeout(tmp_path: Path) -> None:
    """The instance is asked with a few seconds' timeout, not the 30 s of ordinary commands."""
    api = FakeApi()
    api.on("GET", "/api/v1/info", json={"version": "1"})
    state = make_state(tmp_path, api)
    seen: list[float] = []
    factory = state.client_factory

    def spy(inner: CliState) -> lakana.Client:
        seen.append(inner.timeout)
        return factory(inner)

    state.client_factory = spy
    assert invoke(state, "--version").exit_code == 0
    assert seen == [state.version_deadline]


def test_version_says_so_when_the_server_is_unreachable_and_still_succeeds(tmp_path: Path) -> None:
    state = make_state(tmp_path, FakeApi())

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def factory(inner: CliState) -> lakana.Client:
        http = httpx.Client(transport=httpx.MockTransport(refuse), base_url=BASE_URL)
        return lakana.Client(url=BASE_URL, token="lakana_pat_k_s", http_client=http)

    state.client_factory = factory
    result = invoke(state, "--version")

    assert result.exit_code == 0, result.stderr
    assert f"lakana {lakana.__version__}" in result.stdout
    assert f"instance {BASE_URL}: not reachable" in result.stdout
    assert "lakana login" not in result.stdout


def test_version_without_a_profile_says_how_to_configure_one(tmp_path: Path) -> None:
    state = CliState(config_path=tmp_path / "lakana" / "config.toml")

    result = invoke(state, "--version")

    assert result.exit_code == 0, result.stderr
    assert f"lakana {lakana.__version__}" in result.stdout
    assert "instance: no profile is configured; run 'lakana login'" in result.stdout


def test_version_against_a_server_without_the_endpoint(tmp_path: Path) -> None:
    result = invoke(make_state(tmp_path, FakeApi()), "--version")

    assert result.exit_code == 0, result.stderr
    assert "does not report its version" in result.stdout


def test_version_with_a_rejected_credential(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/info", status=401, json={"detail": "bad token"})

    result = invoke(make_state(tmp_path, api), "--version")

    assert result.exit_code == 0, result.stderr
    assert "rejected the credential" in result.stdout
    assert "lakana login" in result.stdout


def test_version_with_any_other_server_error(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/info", status=500, json={"detail": "boom"})

    result = invoke(make_state(tmp_path, api), "--version")

    assert result.exit_code == 0, result.stderr
    assert f"instance {BASE_URL}: could not be asked" in result.stdout


def test_version_with_an_unexpected_transport_error_still_succeeds(tmp_path: Path) -> None:
    state = make_state(tmp_path, FakeApi())

    def broken(inner: CliState) -> lakana.Client:
        raise LakanaConnectionError("cannot connect to http://x")

    state.client_factory = broken
    result = invoke(state, "--version")

    assert result.exit_code == 0, result.stderr
    assert "instance: not reachable" in result.stdout


def test_a_group_without_a_command_is_still_a_usage_error(tmp_path: Path) -> None:
    """`invoke_without_command` must not turn a bare option into a silent success."""
    result = invoke(make_state(tmp_path, FakeApi()), "--output", "json")

    assert result.exit_code == 2


def test_version_reports_a_profile_problem_in_its_own_words(tmp_path: Path) -> None:
    state = make_state(tmp_path, FakeApi())

    def unreadable(inner: CliState) -> lakana.Client:
        raise ConfigError("the profile file is open to other users; run chmod 600 on it")

    state.client_factory = unreadable
    result = invoke(state, "--version")

    assert result.exit_code == 0, result.stderr
    assert "instance: the profile file is open to other users" in result.stdout


def _html(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html>captive portal</html>")


def _object_without_version(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def _list(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=[1])


def _non_string_version(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"version": 3})


@pytest.mark.parametrize("handler", [_html, _object_without_version, _list, _non_string_version])
def test_version_survives_a_url_that_does_not_answer_like_lakana(
    tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Review R3: any 200 body that is not Lakana's JSON still ends in exit 0 and both lines."""
    state = make_state(tmp_path, FakeApi())

    def factory(inner: CliState) -> lakana.Client:
        http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        return lakana.Client(url=BASE_URL, token="lakana_pat_k_s", http_client=http)

    state.client_factory = factory
    result = invoke(state, "--version")

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == f"lakana {lakana.__version__}"
    assert "did not answer like a Lakana server" in lines[1]
    assert "check the URL" in lines[1]


def test_version_makes_one_request_and_does_not_retry(tmp_path: Path) -> None:
    """Review R4: the GET retries of the transport would multiply the bound."""
    state = make_state(tmp_path, FakeApi())
    attempts: list[int] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectTimeout("slow")

    def factory(inner: CliState) -> lakana.Client:
        http = httpx.Client(transport=httpx.MockTransport(refuse), base_url=BASE_URL)
        return lakana.Client(url=BASE_URL, token="lakana_pat_k_s", http_client=http)

    state.client_factory = factory
    result = invoke(state, "--version")

    assert result.exit_code == 0, result.stderr
    assert attempts == [1]


def test_version_gives_up_at_one_overall_deadline(tmp_path: Path) -> None:
    """Review R4: a server that trickles bytes forever must not hold the command."""
    state = make_state(tmp_path, FakeApi())
    state.version_deadline = 0.2
    release = threading.Event()

    class Hanging:
        url = BASE_URL

        def close(self) -> None:
            return None

        def info(self) -> object:
            release.wait(10)
            raise AssertionError("released only at the end of the test")

    def factory(inner: CliState) -> lakana.Client:
        return cast(lakana.Client, Hanging())

    state.client_factory = factory
    started = time.monotonic()
    result = invoke(state, "--version")
    elapsed = time.monotonic() - started

    assert result.exit_code == 0, result.stderr
    assert elapsed < 5
    assert f"lakana {lakana.__version__}" in result.stdout
    assert f"instance {BASE_URL}: did not answer within 0.2 s" in result.stdout
    as_json = invoke(state, "--output", "json", "--version")
    release.set()
    assert json.loads(as_json.stdout)["instance"]["url"] == BASE_URL
