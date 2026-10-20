"""The key cache: discovery on the issuer's host, 1 h freshness, rate-limited refresh, stale use."""

from __future__ import annotations

import threading
from collections.abc import Collection
from pathlib import Path

import pytest
from ci_oidc_test_helpers import (
    AUDIENCE,
    DISCOVERY,
    ISSUER,
    ISSUER_HOST,
    JWKS_URI,
    FakeFetcher,
    b64,
    discovery,
    jwk,
    rsa_key,
)
from pydantic import SecretStr

from lakana_core.ci_oidc import jwks
from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_core.ci_oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.ci_oidc.jwks import JwksCache
from lakana_core.net.https_json import FetchFailed


def b64_uint(value: int) -> str:
    """Encode a number as a JWK's base64url member.

    Parameters
    ----------
    value
        The number.

    Returns
    -------
    str
        The text.
    """
    return b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class Clock:
    """A clock the test moves by hand.

    Parameters
    ----------
    now
        The starting time, in seconds.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        """Tell the time.

        Returns
        -------
        float
            The current time.
        """
        return self.now


def cache_with(
    fetcher: FakeFetcher, clock: Clock | None = None, **config: object
) -> tuple[JwksCache, Clock]:
    """Build a cache for `ISSUER` over a fake fetcher.

    Parameters
    ----------
    fetcher
        The fake fetcher.
    clock
        The clock, or a new one.
    **config
        Further `CiOidcConfig` fields.

    Returns
    -------
    tuple[JwksCache, Clock]
        The cache and its clock.
    """
    clock = clock or Clock()
    settings = CiOidcConfig.model_validate({"issuers": [ISSUER], "audience": AUDIENCE, **config})
    return JwksCache(settings, fetcher=fetcher, clock=clock), clock


def refused(cache: JwksCache, kid: str, issuer: str = ISSUER) -> RefusalCode:
    """Return the code a key lookup is refused with.

    Parameters
    ----------
    cache
        The cache.
    kid
        The key id.
    issuer
        The issuer.

    Returns
    -------
    RefusalCode
        The code.
    """
    with pytest.raises(IdTokenRefused) as info:
        cache.signing_key(issuer, kid)
    return info.value.code


def fetched(fetcher: FakeFetcher) -> list[str]:
    """List the URLs fetched so far.

    Parameters
    ----------
    fetcher
        The fake fetcher.

    Returns
    -------
    list[str]
        The URLs, in order.
    """
    return [url for url, _ in fetcher.calls]


def test_discovery_and_keys_come_from_the_issuer_and_a_fresh_hit_fetches_nothing() -> None:
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("a"), "k1", alg="RS256", use="sig"))
    cache, clock = cache_with(fetcher)
    key = cache.signing_key(ISSUER, "k1")
    assert key.kid == "k1"
    assert key.alg == "RS256"
    assert key.public_key.public_numbers() == rsa_key("a").public_key().public_numbers()
    assert fetcher.calls == [
        (DISCOVERY, frozenset({ISSUER_HOST})),
        (JWKS_URI, frozenset({ISSUER_HOST})),
    ]
    clock.now += jwks.FRESH_SECONDS - 1
    assert cache.signing_key(ISSUER, "k1").kid == "k1"
    assert len(fetcher.calls) == 2


def test_keys_older_than_an_hour_are_fetched_again_and_rotation_replaces_them() -> None:
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("a"), "k1"))
    cache, clock = cache_with(fetcher)
    cache.signing_key(ISSUER, "k1")
    fetcher.publish(jwk(rsa_key("b"), "k2"))
    clock.now += jwks.FRESH_SECONDS
    assert refused(cache, "k1") is RefusalCode.UNKNOWN_KEY
    assert cache.signing_key(ISSUER, "k2").kid == "k2"
    assert len(fetcher.calls) == 4


def test_an_unknown_kid_refreshes_at_most_once_a_minute_per_issuer() -> None:
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("a"), "k1"))
    cache, clock = cache_with(fetcher)
    cache.signing_key(ISSUER, "k1")
    clock.now += jwks.REFRESH_INTERVAL_SECONDS
    for kid in ("x1", "x2", "x3"):
        assert refused(cache, kid) is RefusalCode.UNKNOWN_KEY
    assert len(fetcher.calls) == 4
    clock.now += jwks.REFRESH_INTERVAL_SECONDS - 1
    assert refused(cache, "x4") is RefusalCode.UNKNOWN_KEY
    assert len(fetcher.calls) == 4
    fetcher.publish(jwk(rsa_key("a"), "k1"), jwk(rsa_key("b"), "x5"))
    clock.now += 1
    assert cache.signing_key(ISSUER, "x5").kid == "x5"
    assert len(fetcher.calls) == 6


def test_a_new_kid_right_after_the_first_fetch_waits_for_the_interval() -> None:
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("a"), "k1"))
    cache, _ = cache_with(fetcher)
    assert refused(cache, "new") is RefusalCode.UNKNOWN_KEY
    assert refused(cache, "new") is RefusalCode.UNKNOWN_KEY
    assert len(fetcher.calls) == 2


def test_keys_stay_usable_for_24_hours_after_a_failed_refresh_then_fail_closed() -> None:
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("a"), "k1"))
    cache, clock = cache_with(fetcher)
    cache.signing_key(ISSUER, "k1")
    fetcher.documents.clear()
    clock.now += jwks.FRESH_SECONDS
    assert cache.signing_key(ISSUER, "k1").kid == "k1"
    clock.now += jwks.STALE_SECONDS - 1
    assert cache.signing_key(ISSUER, "k1").kid == "k1"
    clock.now += 1
    assert refused(cache, "k1") is RefusalCode.KEYS_UNAVAILABLE


def test_no_keys_at_all_is_unavailable_and_retried_after_the_interval() -> None:
    fetcher = FakeFetcher()
    cache, clock = cache_with(fetcher)
    assert refused(cache, "k1") is RefusalCode.KEYS_UNAVAILABLE
    assert refused(cache, "k1") is RefusalCode.KEYS_UNAVAILABLE
    assert fetched(fetcher) == [DISCOVERY]
    fetcher.publish(jwk(rsa_key("a"), "k1"))
    clock.now += jwks.REFRESH_INTERVAL_SECONDS
    assert cache.signing_key(ISSUER, "k1").kid == "k1"


def test_an_issuer_that_is_not_configured_is_never_fetched() -> None:
    fetcher = FakeFetcher()
    cache, _ = cache_with(fetcher)
    assert refused(cache, "k1", issuer="https://evil.example") is RefusalCode.ISSUER_NOT_ALLOWED
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "jwks_uri",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://evil.example/jwks",
        "http://token.actions.githubusercontent.com/jwks",
        "https://user@token.actions.githubusercontent.com/jwks",
        "https://token.actions.githubusercontent.com.evil.example/jwks",
        "not a url",
        "https://[token.actions.githubusercontent.com/jwks",
        "",
        42,
    ],
)
def test_a_jwks_uri_off_the_issuers_host_is_never_fetched(jwks_uri: object) -> None:
    fetcher = FakeFetcher({DISCOVERY: {"issuer": ISSUER, "jwks_uri": jwks_uri}})
    cache, _ = cache_with(fetcher)
    assert refused(cache, "k1") is RefusalCode.KEYS_UNAVAILABLE
    assert fetched(fetcher) == [DISCOVERY]


def test_an_allow_listed_host_may_serve_the_keys() -> None:
    other = "https://keys.example.com/jwks"
    fetcher = FakeFetcher({DISCOVERY: discovery(other), other: {"keys": [jwk(rsa_key("a"), "k1")]}})
    cache, _ = cache_with(fetcher, allow_hosts=["keys.example.com"])
    assert cache.signing_key(ISSUER, "k1").kid == "k1"
    assert fetcher.calls[1] == (other, frozenset({ISSUER_HOST, "keys.example.com"}))


@pytest.mark.parametrize(
    "document",
    [
        discovery(issuer=ISSUER + "/"),
        discovery(issuer="https://evil.example"),
        {"jwks_uri": JWKS_URI},
        ["not", "an", "object"],
    ],
)
def test_a_discovery_document_for_another_issuer_is_refused(document: object) -> None:
    fetcher = FakeFetcher({DISCOVERY: document, JWKS_URI: {"keys": [jwk(rsa_key("a"), "k1")]}})
    cache, _ = cache_with(fetcher)
    assert refused(cache, "k1") is RefusalCode.KEYS_UNAVAILABLE
    assert fetched(fetcher) == [DISCOVERY]


@pytest.mark.parametrize("key_set", [[], {"keys": "no"}, {"nokeys": []}, "text"])
def test_a_key_set_that_is_not_one_is_refused(key_set: object) -> None:
    fetcher = FakeFetcher({DISCOVERY: discovery(), JWKS_URI: key_set})
    cache, _ = cache_with(fetcher)
    assert refused(cache, "k1") is RefusalCode.KEYS_UNAVAILABLE


def test_two_keys_with_one_kid_make_the_whole_set_unusable() -> None:
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("a"), "k1"), jwk(rsa_key("b"), "k1"))
    cache, _ = cache_with(fetcher)
    assert refused(cache, "k1") is RefusalCode.KEYS_UNAVAILABLE


def test_keys_that_cannot_verify_are_left_out() -> None:
    private = jwk(rsa_key("a"), "private", d="AQAB")
    fetcher = FakeFetcher()
    fetcher.publish(
        jwk(rsa_key("a"), "good"),
        {"kty": "EC", "kid": "ec", "crv": "P-256", "x": "AA", "y": "AA"},
        {"kty": "oct", "kid": "secret", "k": "c2VjcmV0"},
        {**jwk(rsa_key("a"), "enc"), "use": "enc"},
        {**jwk(rsa_key("a"), "broken"), "n": "!!"},
        {**jwk(rsa_key("a"), "nokid"), "kid": 7},
        {**jwk(rsa_key("a"), "intn"), "n": 5},
        {**jwk(rsa_key("a"), "huge"), "n": b64_uint((1 << 8200) - 1)},
        private,
        "not an object",
    )
    cache, _ = cache_with(fetcher)
    assert cache.signing_key(ISSUER, "good").kid == "good"
    for kid in ("ec", "secret", "enc", "broken", "private", "7", "intn", "huge"):
        assert refused(cache, kid) is RefusalCode.UNKNOWN_KEY, kid


def test_a_failed_fetch_is_logged_with_the_issuer_and_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetcher = FakeFetcher({DISCOVERY: FetchFailed("the host is a private or internal address")})
    cache, _ = cache_with(fetcher)
    refused(cache, "k1")
    assert ISSUER in caplog.text
    assert "the host is a private or internal address" in caplog.text


def test_the_default_fetcher_uses_the_operator_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Collection[str], object, object]] = []

    def fake(
        url: str, *, allowed_hosts: Collection[str], proxy: object, ca_bundle: object
    ) -> object:
        seen.append((url, allowed_hosts, proxy, ca_bundle))
        raise FetchFailed("offline")

    monkeypatch.setattr(jwks, "fetch_json", fake)
    settings = CiOidcConfig(
        issuers=[ISSUER],
        audience=AUDIENCE,
        proxy=SecretStr("http://proxy.corp.test:3128"),
        ca_bundle=Path("/certs/ca.pem"),
    )
    cache = JwksCache(settings)
    with pytest.raises(IdTokenRefused):
        cache.signing_key(ISSUER, "k1")
    url, allowed, proxy, bundle = seen[0]
    assert (url, set(allowed)) == (DISCOVERY, {ISSUER_HOST})
    assert proxy == settings.proxy and bundle == settings.ca_bundle


def test_concurrent_lookups_of_an_unknown_kid_fetch_once() -> None:
    gate = threading.Event()
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("a"), "k1"))

    class Slow(FakeFetcher):
        """A fetcher that waits for the gate before answering."""

        def __call__(self, url: str, allowed_hosts: Collection[str]) -> object:
            """Wait, then answer.

            Parameters
            ----------
            url
                The URL.
            allowed_hosts
                The allowed hosts.

            Returns
            -------
            object
                The document.
            """
            gate.wait(5)
            return super().__call__(url, allowed_hosts)

    slow = Slow(fetcher.documents)
    cache, _ = cache_with(slow)
    results: list[str] = []

    def look() -> None:
        """Look the key up and record its kid."""
        results.append(cache.signing_key(ISSUER, "k1").kid)

    threads = [threading.Thread(target=look) for _ in range(4)]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join(5)
    assert results == ["k1"] * 4
    assert len(slow.calls) == 2
