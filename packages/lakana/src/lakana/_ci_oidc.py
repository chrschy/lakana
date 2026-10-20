"""Getting a CI job's OIDC ID token, for `Client.from_ci_oidc`.

GitHub Actions hands a job with ``permissions: id-token: write`` a URL and a request credential
(``ACTIONS_ID_TOKEN_REQUEST_URL``, ``ACTIONS_ID_TOKEN_REQUEST_TOKEN``) to ask for a new ID token
for an audience; GitLab CI puts the ID token a job declares under ``id_tokens:`` into a variable.
The ID token stays in memory: it is never written, logged, or part of an error message.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import httpx

from lakana.errors import AuthenticationError, ConfigError, InvalidResponse, LakanaConnectionError

Provider = Literal["github", "gitlab"]
"""The CI providers whose ID token the client can get by itself."""

PROVIDER_VARIABLE = "LAKANA_OIDC"
"""Names the provider: ``github`` or ``gitlab``."""

TOKEN_VARIABLE = "LAKANA_OIDC_TOKEN"  # nosec B105 -- the name of a variable, not a credential
"""Holds the ID token itself: GitLab's ``id_tokens:`` entry, or any provider's token."""

AUDIENCE_VARIABLE = "LAKANA_OIDC_AUDIENCE"
"""The audience to ask GitHub for, when it is not the server's URL."""

_GITHUB_URL = "ACTIONS_ID_TOKEN_REQUEST_URL"
_GITHUB_REQUEST_TOKEN = "ACTIONS_ID_TOKEN_REQUEST_TOKEN"  # nosec B105 -- a variable's name

_UNKNOWN_PROVIDER = (
    f"{PROVIDER_VARIABLE} (or provider=) must be github or gitlab. Set "
    f"{PROVIDER_VARIABLE}=github on GitHub Actions or {PROVIDER_VARIABLE}=gitlab on GitLab CI, "
    f"or pass the ID token itself in {TOKEN_VARIABLE}."
)
_NO_SOURCE = (
    "No CI ID token to exchange: set LAKANA_OIDC=github on GitHub Actions, LAKANA_OIDC=gitlab "
    f"on GitLab CI (with the job's id_tokens: entry named {TOKEN_VARIABLE}), or put the ID "
    f"token in {TOKEN_VARIABLE}."
)
_NO_GITHUB_PERMISSION = (
    f"This job cannot ask GitHub Actions for an ID token: {_GITHUB_URL} and "
    f"{_GITHUB_REQUEST_TOKEN} are not set. Give the job `permissions: id-token: write` in the "
    "workflow; a pull request from a fork never gets one."
)
_NO_GITLAB_TOKEN = (
    f"{PROVIDER_VARIABLE}=gitlab reads the job's ID token from {TOKEN_VARIABLE}, which is not "
    f"set. Declare it in the job in .gitlab-ci.yml: `id_tokens: {TOKEN_VARIABLE}: aud: "
    "<the server's audience>`."
)
_EMPTY_ANSWER = (
    "GitHub Actions answered without an ID token. Run the job again; if it keeps happening, check "
    "the runner's status."
)
GITLAB_ONE_SUBMISSION = (
    " A GitLab job gets each ID token it declares once, and each ID token is exchanged once, so "
    "a job submits once per ID token. To submit twice in one job, a dry run and then a deploy, "
    "declare a second token under id_tokens: (for example LAKANA_OIDC_TOKEN_2) and run the "
    f"second command with {TOKEN_VARIABLE} set to it, or run each in a job of its own."
)
"""Added to the server's refusal of a replayed ID token when it came from a variable."""


def resolve_provider(provider: str | None, environ: Mapping[str, str]) -> Provider | None:
    """Decide where the ID token comes from.

    Parameters
    ----------
    provider
        The provider asked for; `None` reads `LAKANA_OIDC`.
    environ
        The environment.

    Returns
    -------
    {"github", "gitlab"} or None
        The provider; `None` when neither names one (the token is then `LAKANA_OIDC_TOKEN`).

    Raises
    ------
    ConfigError
        If the provider is neither ``github`` nor ``gitlab``; the value is never echoed, since a
        token pasted into the wrong variable would be.
    """
    chosen = provider if provider is not None else environ.get(PROVIDER_VARIABLE)
    if chosen is None:
        return None
    if chosen == "github":
        return "github"
    if chosen == "gitlab":
        return "gitlab"
    raise ConfigError(_UNKNOWN_PROVIDER)


def _github_id_token(
    audience: str,
    environ: Mapping[str, str],
    *,
    timeout: float,
    http_client: httpx.Client | None,
) -> str:
    """Ask the GitHub Actions runner for a new ID token for `audience`.

    Parameters
    ----------
    audience
        The audience the Lakana server expects.
    environ
        The environment with the runner's request URL and credential.
    timeout
        Request timeout in seconds.
    http_client
        A client to send through; a new one, closed afterwards, when `None`.

    Returns
    -------
    str
        The ID token.

    Raises
    ------
    ConfigError
        If the job has no ``id-token: write`` permission.
    AuthenticationError
        If the runner refuses (the message names the status, never the body).
    InvalidResponse
        If the answer holds no ID token.
    LakanaConnectionError
        If the runner's service cannot be reached.
    """
    url = environ.get(_GITHUB_URL)
    credential = environ.get(_GITHUB_REQUEST_TOKEN)
    if not url or not credential:
        raise ConfigError(_NO_GITHUB_PERMISSION)
    client = http_client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        # The runner's URL carries a query of its own (`api-version`); keep it.
        response = client.get(
            httpx.URL(url).copy_merge_params({"audience": audience}),
            headers={"Authorization": f"Bearer {credential}"},
        )
    except httpx.HTTPError as exc:
        raise LakanaConnectionError(
            f"Could not reach GitHub Actions' ID token service ({type(exc).__name__}). Run the "
            "job again; if it keeps happening, check the runner's network."
        ) from None
    finally:
        if http_client is None:
            client.close()
    if response.status_code != 200:
        raise AuthenticationError(
            f"GitHub Actions refused to issue an ID token (HTTP {response.status_code}). Check "
            "that the job has `permissions: id-token: write`.",
            status=response.status_code,
        )
    try:
        value = response.json().get("value")
    except (ValueError, AttributeError):
        value = None
    if not isinstance(value, str) or not value:
        raise InvalidResponse(_EMPTY_ANSWER)
    return value


def id_token_for(
    provider: Provider | None,
    *,
    audience: str,
    environ: Mapping[str, str],
    timeout: float = 30.0,
    http_client: httpx.Client | None = None,
) -> str:
    """Get the CI job's ID token from its provider.

    Parameters
    ----------
    provider
        ``github`` asks the runner for a new one; ``gitlab`` and `None` read
        `LAKANA_OIDC_TOKEN`.
    audience
        The audience to ask GitHub for.
    environ
        The environment.
    timeout
        Request timeout in seconds for GitHub's runner.
    http_client
        A client to send GitHub's request through.

    Returns
    -------
    str
        The ID token, without surrounding whitespace.

    Raises
    ------
    ConfigError
        If the provider's variables are missing; the message says what to add.
    """
    if provider == "github":
        return _github_id_token(audience, environ, timeout=timeout, http_client=http_client)
    token = environ.get(TOKEN_VARIABLE, "").strip()
    if token:
        return token
    raise ConfigError(_NO_GITLAB_TOKEN if provider == "gitlab" else _NO_SOURCE)
