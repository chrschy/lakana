"""`Client.from_ci_oidc`: a client for a CI job, from its OIDC ID token, without a stored secret."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from lakana._ci_oidc import id_token_for
from lakana._models import CiTokenScope, Whoami
from lakana.client import Client
from lakana.errors import (
    AuthenticationError,
    ConfigError,
    InvalidResponse,
    LakanaConnectionError,
    LakanaError,
)

URL = "https://lakana.example.com"
RUNNER = "https://runner.example/idtoken?api-version=2.0"
REQUEST_TOKEN = "runner-request-credential"
ID_TOKEN = "header.claims-of-the-job.signature-of-the-provider"
CI_SECRET = "cisecretpart"
CI_TOKEN = f"lakana_ci_k1_{CI_SECRET}"
RULE_ID = "00000000-0000-0000-0000-000000000001"
_ISSUED = {
    "token": CI_TOKEN,
    "expires_at": "2026-10-20T13:00:00+00:00",
    "principal": "ci-prod",
    "environment": "prod",
    "repository": "analytics",
    "kinds": ["deploy"],
    "sha": "0" * 40,
    "rule_id": RULE_ID,
}
_OIDC_VARIABLES = (
    "LAKANA_OIDC",
    "LAKANA_OIDC_TOKEN",
    "LAKANA_OIDC_AUDIENCE",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
)


@pytest.fixture(autouse=True)
def _no_ci_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without the variables a CI runner sets.

    Parameters
    ----------
    monkeypatch
        The fixture.
    """
    for name in _OIDC_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _github_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the variables GitHub Actions gives a job with ``id-token: write``.

    Parameters
    ----------
    monkeypatch
        The fixture.
    """
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", RUNNER)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", REQUEST_TOKEN)


Handler = Callable[[httpx.Request], httpx.Response]


def _http(
    seen: list[httpx.Request],
    *,
    runner: Handler | None = None,
    exchange: Handler | None = None,
) -> httpx.Client:
    """Fake the runner's ID token service and the Lakana server in one client.

    Parameters
    ----------
    seen
        Collects every request.
    runner
        Answers the runner's requests; a fresh ID token by default.
    exchange
        Answers the exchange; the issued CI token by default.

    Returns
    -------
    httpx.Client
        The client.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        """Answer one request.

        Parameters
        ----------
        request
            The request.

        Returns
        -------
        httpx.Response
            The answer.
        """
        seen.append(request)
        if request.url.host == "runner.example":
            return runner(request) if runner else httpx.Response(200, json={"value": ID_TOKEN})
        if request.url.path == "/api/v1/auth/oidc/token":
            return exchange(request) if exchange else httpx.Response(200, json=_ISSUED)
        return httpx.Response(
            200, json={"name": "ci-prod", "type": "SERVICE_ACCOUNT", "task_token": None}
        )

    return httpx.Client(transport=httpx.MockTransport(handle), base_url=URL)


def _never_shown(*texts: str) -> None:
    """Assert that neither the ID token nor the CI token's secret is in any of `texts`.

    Parameters
    ----------
    *texts
        What a user or a log could see.
    """
    for text in texts:
        assert ID_TOKEN not in text
        assert CI_SECRET not in text
        assert REQUEST_TOKEN not in text


def test_github_asks_the_runner_for_an_id_token_for_the_instance_and_exchanges_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _github_runner(monkeypatch)
    seen: list[httpx.Request] = []
    caplog.set_level(logging.DEBUG)
    client = Client.from_ci_oidc(
        URL, environment="prod", repository="analytics", provider="github", http_client=_http(seen)
    )
    runner, exchanged = seen
    assert runner.url.params["audience"] == URL
    assert runner.url.params["api-version"] == "2.0"
    assert runner.headers["authorization"] == f"Bearer {REQUEST_TOKEN}"
    assert "authorization" not in exchanged.headers
    assert json.loads(exchanged.content) == {
        "id_token": ID_TOKEN,
        "environment": "prod",
        "repository": "analytics",
    }
    client.whoami()
    assert seen[-1].headers["authorization"] == f"Bearer {CI_TOKEN}"
    assert client.ci_token is not None and client.ci_token.rule_id == RULE_ID
    assert client.url == URL
    _never_shown(repr(client), repr(client.ci_token), caplog.text)


def test_the_audience_can_be_set_when_the_operator_chose_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _github_runner(monkeypatch)
    seen: list[httpx.Request] = []
    Client.from_ci_oidc(
        URL,
        environment="prod",
        repository="analytics",
        provider="github",
        audience="lakana-prod",
        http_client=_http(seen),
    )
    monkeypatch.setenv("LAKANA_OIDC_AUDIENCE", "from-the-environment")
    Client.from_ci_oidc(
        URL, environment="prod", repository="analytics", provider="github", http_client=_http(seen)
    )
    assert [r.url.params["audience"] for r in seen if r.url.host == "runner.example"] == [
        "lakana-prod",
        "from-the-environment",
    ]


def test_each_client_asks_github_for_a_new_id_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _github_runner(monkeypatch)
    seen: list[httpx.Request] = []
    for kind in ("dry run", "deploy"):
        del kind
        Client.from_ci_oidc(
            URL,
            environment="prod",
            repository="analytics",
            provider="github",
            http_client=_http(seen),
        )
    hosts = [request.url.host for request in seen]
    assert hosts == ["runner.example", "lakana.example.com"] * 2


def test_github_without_the_id_token_permission_says_what_to_add() -> None:
    with pytest.raises(ConfigError, match="id-token: write") as caught:
        Client.from_ci_oidc(URL, environment="prod", repository="a", provider="github")
    assert "ACTIONS_ID_TOKEN_REQUEST_URL" in str(caught.value)


@pytest.mark.parametrize(
    ("answer", "error", "words"),
    [
        (httpx.Response(403, text=f"denied {ID_TOKEN}"), AuthenticationError, "HTTP 403"),
        (httpx.Response(200, text=f"not json {ID_TOKEN}"), InvalidResponse, "without an ID token"),
        (httpx.Response(200, json={"value": ""}), InvalidResponse, "without an ID token"),
        (httpx.Response(200, json=[ID_TOKEN]), InvalidResponse, "without an ID token"),
    ],
)
def test_a_refusal_or_an_odd_answer_from_the_runner_never_echoes_it(
    monkeypatch: pytest.MonkeyPatch,
    answer: httpx.Response,
    error: type[LakanaError],
    words: str,
) -> None:
    _github_runner(monkeypatch)
    seen: list[httpx.Request] = []
    with pytest.raises(error, match=words) as caught:
        Client.from_ci_oidc(
            URL,
            environment="prod",
            repository="a",
            provider="github",
            http_client=_http(seen, runner=lambda request: answer),
        )
    assert len(seen) == 1
    _never_shown("".join(traceback.format_exception(caught.value)))


def test_an_unreachable_runner_is_a_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _github_runner(monkeypatch)

    def unreachable(request: httpx.Request) -> httpx.Response:
        """Fail like a network that does not answer.

        Parameters
        ----------
        request
            The request.

        Raises
        ------
        httpx.ConnectError
            Always.
        """
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(LakanaConnectionError, match="GitHub Actions"):
        Client.from_ci_oidc(
            URL,
            environment="prod",
            repository="a",
            provider="github",
            http_client=_http([], runner=unreachable),
        )


def test_without_a_client_of_its_own_the_runner_is_asked_over_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    fake = _http(seen)
    built: list[dict[str, object]] = []

    def build(**options: object) -> httpx.Client:
        """Stand in for `httpx.Client`, recording its options.

        Parameters
        ----------
        **options
            The options.

        Returns
        -------
        httpx.Client
            The fake.
        """
        built.append(options)
        return fake

    monkeypatch.setattr("lakana._ci_oidc.httpx.Client", build)
    environ = {"ACTIONS_ID_TOKEN_REQUEST_URL": RUNNER, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t"}
    token = id_token_for("github", audience=URL, environ=environ, timeout=7, http_client=None)
    assert token == ID_TOKEN
    assert built == [{"timeout": 7, "follow_redirects": False}]
    assert fake.is_closed


def test_gitlab_reads_the_id_token_from_its_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKANA_OIDC_TOKEN", f"  {ID_TOKEN}\n")
    seen: list[httpx.Request] = []
    Client.from_ci_oidc(
        URL, environment="prod", repository="a", provider="gitlab", http_client=_http(seen)
    )
    [exchanged] = seen
    assert json.loads(exchanged.content)["id_token"] == ID_TOKEN


def test_gitlab_without_the_variable_says_how_to_declare_it() -> None:
    with pytest.raises(ConfigError, match="id_tokens") as caught:
        Client.from_ci_oidc(URL, environment="prod", repository="a", provider="gitlab")
    assert "LAKANA_OIDC_TOKEN" in str(caught.value)


def test_an_explicit_id_token_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _github_runner(monkeypatch)
    monkeypatch.setenv("LAKANA_OIDC_TOKEN", "another")
    seen: list[httpx.Request] = []
    Client.from_ci_oidc(
        URL,
        environment="prod",
        repository="a",
        provider="github",
        id_token=ID_TOKEN,
        http_client=_http(seen),
    )
    [exchanged] = seen
    assert json.loads(exchanged.content)["id_token"] == ID_TOKEN


def test_without_a_provider_the_environment_chooses(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    monkeypatch.setenv("LAKANA_OIDC", "github")
    _github_runner(monkeypatch)
    Client.from_ci_oidc(URL, environment="prod", repository="a", http_client=_http(seen))
    assert seen[0].url.host == "runner.example"
    monkeypatch.delenv("LAKANA_OIDC")
    monkeypatch.setenv("LAKANA_OIDC_TOKEN", ID_TOKEN)
    Client.from_ci_oidc(URL, environment="prod", repository="a", http_client=_http(seen))
    assert json.loads(seen[-1].content)["id_token"] == ID_TOKEN
    assert len(seen) == 3


def test_without_any_source_the_error_names_every_way(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="LAKANA_OIDC=github") as caught:
        Client.from_ci_oidc(URL, environment="prod", repository="a")
    assert "LAKANA_OIDC_TOKEN" in str(caught.value)


@pytest.mark.parametrize("value", [ID_TOKEN, "GitHub", ""])
def test_an_unknown_provider_is_refused_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LAKANA_OIDC", value)
    with pytest.raises(ConfigError, match="github or gitlab") as caught:
        Client.from_ci_oidc(URL, environment="prod", repository="a")
    _never_shown(str(caught.value))
    with pytest.raises(ConfigError, match="github or gitlab"):
        Client.from_ci_oidc(URL, environment="prod", repository="a", provider=value)  # ty: ignore[invalid-argument-type]


def test_an_empty_explicit_id_token_is_refused() -> None:
    with pytest.raises(ConfigError, match="empty"):
        Client.from_ci_oidc(URL, environment="prod", repository="a", id_token=" ")


_REPLAYED = (
    "This ID token was exchanged before; each ID token works once. Request a new one in the CI "
    "job for each exchange."
)


@pytest.mark.parametrize("provider", ["gitlab", None])
def test_a_second_exchange_of_one_gitlab_token_says_how_to_get_two(
    monkeypatch: pytest.MonkeyPatch, provider: str | None
) -> None:
    monkeypatch.setenv("LAKANA_OIDC_TOKEN", ID_TOKEN)

    def replayed(request: httpx.Request) -> httpx.Response:
        """Refuse like a server that has seen this ID token.

        Parameters
        ----------
        request
            The request.

        Returns
        -------
        httpx.Response
            The refusal.
        """
        return httpx.Response(401, json={"detail": _REPLAYED, "type": "AuthenticationError"})

    with pytest.raises(AuthenticationError) as caught:
        Client.from_ci_oidc(
            URL,
            environment="prod",
            repository="a",
            provider=provider,  # ty: ignore[invalid-argument-type]
            http_client=_http([], exchange=replayed),
        )
    message = str(caught.value)
    assert message.startswith(_REPLAYED)
    assert "submits once" in message and "id_tokens" in message
    assert caught.value.status == 401
    _never_shown("".join(traceback.format_exception(caught.value)))


def test_other_refusals_are_passed_on_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKANA_OIDC_TOKEN", ID_TOKEN)

    def refused(request: httpx.Request) -> httpx.Response:
        """Refuse the ID token for its audience.

        Parameters
        ----------
        request
            The request.

        Returns
        -------
        httpx.Response
            The refusal.
        """
        return httpx.Response(401, json={"detail": "another audience"})

    with pytest.raises(AuthenticationError) as caught:
        Client.from_ci_oidc(
            URL,
            environment="prod",
            repository="a",
            provider="gitlab",
            http_client=_http([], exchange=refused),
        )
    assert str(caught.value) == "another audience"


def test_whoami_reads_a_ci_tokens_scope() -> None:
    body = {
        "name": "ci-prod",
        "type": "SERVICE_ACCOUNT",
        "task_token": None,
        "ci_token": {
            "rule_id": RULE_ID,
            "environment": "prod",
            "repository": "analytics",
            "kinds": ["deploy", "dry_run"],
            "sha": "0" * 40,
            "expires_at": "2026-10-20T13:00:00Z",
            "used": True,
        },
    }
    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        base_url=URL,
    )
    with Client(url=URL, token=CI_TOKEN, http_client=http) as client:
        assert client.whoami() == Whoami(
            name="ci-prod",
            type="SERVICE_ACCOUNT",
            task_token=None,
            ci_token=CiTokenScope(
                rule_id=RULE_ID,
                environment="prod",
                repository="analytics",
                kinds=["deploy", "dry_run"],
                sha="0" * 40,
                expires_at=datetime(2026, 10, 20, 13, tzinfo=UTC),
                used=True,
            ),
        )
        assert client.ci_token is None
