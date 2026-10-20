"""An ID token is checked in a fixed order; every refusal has a code and never quotes the token."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import cast

import jwt
import pytest
from ci_oidc_test_helpers import (
    AUDIENCE,
    ISSUER,
    FakeFetcher,
    b64,
    jwk,
    rsa_key,
    unsigned,
)
from cryptography.hazmat.primitives import serialization

from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_core.ci_oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.ci_oidc.jwks import JwksCache
from lakana_core.ci_oidc.verification import IdTokenVerifier, VerifiedIdToken

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
T = int(NOW.timestamp())


def claims(**changes: object) -> dict[str, object]:
    """Return the claims of a good GitHub Actions token, with changes.

    Parameters
    ----------
    **changes
        Claims to set; a value of `None` removes the claim.

    Returns
    -------
    dict[str, object]
        The claims.
    """
    base: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "repo:org/repo:ref:refs/heads/main",
        "jti": "8a1b5c3e-0d1f-4c8e-9b7a-1f2e3d4c5b6a",
        "iat": T - 30,
        "nbf": T - 30,
        "exp": T + 270,
        "repository_id": "123456",
    }
    base.update(changes)
    return {name: value for name, value in base.items() if value is not None}


def sign(
    payload: dict[str, object] | None = None,
    *,
    key: str = "main",
    bits: int = 2048,
    alg: str = "RS256",
    kid: str | None = "k1",
    **headers: object,
) -> str:
    """Sign a token with one of the test keys.

    Parameters
    ----------
    payload
        The claims; the good ones by default.
    key
        The test key's name.
    bits
        The test key's size.
    alg
        The algorithm.
    kid
        The key id in the header, or `None` for none.
    **headers
        Further header members.

    Returns
    -------
    str
        The token.
    """
    extra = dict(headers)
    if kid is not None:
        extra["kid"] = kid
    return jwt.encode(payload or claims(), rsa_key(key, bits), algorithm=alg, headers=extra)


@pytest.fixture
def fetcher() -> FakeFetcher:
    """Publish the main key as ``k1`` at the issuer.

    Returns
    -------
    FakeFetcher
        The fetcher.
    """
    published = FakeFetcher()
    published.publish(
        jwk(rsa_key("main"), "k1", alg="RS256"),
        jwk(rsa_key("weak", 1024), "weak"),
        jwk(rsa_key("pss"), "pss", alg="PS256"),
        jwk(rsa_key("other"), "any"),
    )
    return published


def verifier(fetcher: FakeFetcher, **config: object) -> IdTokenVerifier:
    """Build a verifier for `ISSUER` and `AUDIENCE` over a fake fetcher.

    Parameters
    ----------
    fetcher
        The fake fetcher.
    **config
        Further `CiOidcConfig` fields.

    Returns
    -------
    IdTokenVerifier
        The verifier.
    """
    settings = CiOidcConfig.model_validate({"issuers": [ISSUER], "audience": AUDIENCE, **config})
    return IdTokenVerifier(settings, JwksCache(settings, fetcher=fetcher))


def refusal(check: IdTokenVerifier, token: str, now: datetime = NOW) -> RefusalCode:
    """Return the code a token is refused with, checking the message never quotes it.

    Parameters
    ----------
    check
        The verifier.
    token
        The token.
    now
        The time of the check.

    Returns
    -------
    RefusalCode
        The code.
    """
    with pytest.raises(IdTokenRefused) as info:
        check.verify_id_token(token, now=now)
    error = info.value
    shown = str(error) + repr(error) + error.message
    for part in token.split("."):
        if len(part) > 8:
            assert part not in shown
    assert "repo:org/repo" not in shown
    assert error.__cause__ is None
    return error.code


def test_a_good_token_is_verified(fetcher: FakeFetcher) -> None:
    result = verifier(fetcher).verify_id_token(sign(), now=NOW)
    assert result == VerifiedIdToken(
        claims=claims(),
        issuer=ISSUER,
        subject="repo:org/repo:ref:refs/heads/main",
        jti="8a1b5c3e-0d1f-4c8e-9b7a-1f2e3d4c5b6a",
        expires_at=NOW + timedelta(seconds=270),
    )


def test_an_audience_list_that_contains_ours_is_accepted(fetcher: FakeFetcher) -> None:
    token = sign(claims(aud=["https://other.example", AUDIENCE]))
    assert verifier(fetcher).verify_id_token(token, now=NOW).issuer == ISSUER


def test_another_allowed_algorithm_and_a_key_without_alg_verify(fetcher: FakeFetcher) -> None:
    check = verifier(fetcher, algorithms=["RS256", "PS256"])
    assert check.verify_id_token(sign(key="pss", alg="PS256", kid="pss"), now=NOW)
    assert check.verify_id_token(sign(key="other", kid="any"), now=NOW)


def test_the_feature_off_refuses_everything_without_a_fetch(fetcher: FakeFetcher) -> None:
    settings = CiOidcConfig()
    check = IdTokenVerifier(settings, JwksCache(settings, fetcher=fetcher))
    assert refusal(check, sign()) is RefusalCode.DISABLED
    assert fetcher.calls == []


def test_the_default_cache_is_built_from_the_settings() -> None:
    settings = CiOidcConfig()
    assert refusal(IdTokenVerifier(settings), sign()) is RefusalCode.DISABLED


def test_a_naive_time_is_a_programming_error(fetcher: FakeFetcher) -> None:
    with pytest.raises(ValueError, match="time zone"):
        verifier(fetcher).verify_id_token(sign(), now=NOW.replace(tzinfo=None))


# -- before any network call -----------------------------------------------------------------


def test_an_oversize_token_is_refused_before_it_is_parsed(fetcher: FakeFetcher) -> None:
    token = sign(claims(pad="x" * 17000))
    assert refusal(verifier(fetcher), token) is RefusalCode.TOKEN_TOO_LARGE
    assert refusal(verifier(fetcher), "a" * (16 * 1024 + 1)) is RefusalCode.TOKEN_TOO_LARGE
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "token",
    [
        "",
        "abc",
        "a.b",
        "a.b.c.d",
        "eyJh.eyJh.c2ln\n",
        "a b.c.d",
        ".payload.sig",
        "eyJ.eyJ.sig",
        b64(b"[1]") + "." + b64(b"{}") + ".c2ln",
        b64(b"[" * 6000 + b"]" * 6000) + "." + b64(b"{}") + ".c2ln",
        b64(b"\xff\xfe") + "." + b64(b"{}") + ".c2ln",
        b64(b'{"alg":"RS256","alg":"none","kid":"k1"}') + "." + b64(b"{}") + ".c2ln",
        unsigned({"alg": "RS256", "kid": "k1"}, {}).rsplit(".", 2)[0] + ".e30x.c2ln",
        unsigned({"alg": "RS256", "kid": "k1"}, {}).split(".")[0] + "." + b64(b"[]") + ".c2ln",
        unsigned({"alg": "RS256", "kid": "k1"}, {}).split(".")[0]
        + "."
        + b64(b'{"iss":"https://evil.example","iss":"' + ISSUER.encode() + b'"}')
        + ".c2ln",
    ],
)
def test_a_token_that_is_not_a_well_formed_jws_is_refused(fetcher: FakeFetcher, token: str) -> None:
    assert refusal(verifier(fetcher), token) is RefusalCode.MALFORMED
    assert fetcher.calls == []


def test_a_good_token_with_a_trailing_newline_is_refused(fetcher: FakeFetcher) -> None:
    assert refusal(verifier(fetcher), sign() + "\n") is RefusalCode.MALFORMED


def test_a_non_text_token_is_refused(fetcher: FakeFetcher) -> None:
    with pytest.raises(IdTokenRefused) as info:
        verifier(fetcher).verify_id_token(cast("str", b"bytes"), now=NOW)
    assert info.value.code is RefusalCode.MALFORMED


@pytest.mark.parametrize("alg", ["none", "None", "HS256", "RS384", "ES256", None, 5])
def test_an_algorithm_off_the_allow_list_is_refused(fetcher: FakeFetcher, alg: object) -> None:
    header: dict[str, object] = {"kid": "k1", "typ": "JWT"}
    if alg is not None:
        header["alg"] = alg
    assert (
        refusal(verifier(fetcher), unsigned(header, claims())) is RefusalCode.ALGORITHM_NOT_ALLOWED
    )
    assert fetcher.calls == []


def test_hs256_signed_with_the_public_key_is_refused_even_when_allowed_by_mistake(
    fetcher: FakeFetcher,
) -> None:
    public_pem = (
        rsa_key("main")
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    head = b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
    body = b64(json.dumps(claims()).encode())
    mac = hmac.new(public_pem, f"{head}.{body}".encode(), hashlib.sha256).digest()
    token = f"{head}.{body}.{b64(mac)}"
    assert refusal(verifier(fetcher), token) is RefusalCode.ALGORITHM_NOT_ALLOWED
    # The settings refuse HS256 at start; a verifier that was handed it anyway still never
    # verifies an HMAC, whether or not the issuer's key names its algorithm.
    check = verifier(fetcher)
    check._algorithms = frozenset({"RS256", "HS256"})
    assert refusal(check, token) is RefusalCode.KEY_MISMATCH
    any_key = f"{b64(json.dumps({'alg': 'HS256', 'kid': 'any'}).encode())}.{body}.{b64(mac)}"
    assert refusal(check, any_key) is RefusalCode.KEY_MISMATCH


@pytest.mark.parametrize(
    "header",
    [
        {"jku": "https://evil.example/jwks"},
        {"x5u": "https://evil.example/cert"},
        {"x5c": ["MIIB"]},
        {"jwk": jwk(rsa_key("attacker"), "k1")},
        {"crit": ["exp"]},
        {"crit": []},
    ],
)
def test_a_header_that_supplies_a_key_or_a_critical_extension_is_refused(
    fetcher: FakeFetcher, header: dict[str, object]
) -> None:
    headers = {"kid": "k1", **header}
    token = jwt.encode(claims(), rsa_key("attacker"), algorithm="RS256", headers=headers)
    assert refusal(verifier(fetcher), token) is RefusalCode.HEADER_NOT_ALLOWED
    assert fetcher.calls == []


@pytest.mark.parametrize("kid", [None, "", 7, "k" * 257])
def test_a_missing_or_odd_kid_is_refused(fetcher: FakeFetcher, kid: object) -> None:
    header: dict[str, object] = {"alg": "RS256"}
    if kid is not None:
        header["kid"] = kid
    token = unsigned(header, claims(), b"sig")
    assert refusal(verifier(fetcher), token) is RefusalCode.MISSING_KEY_ID
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "iss",
    [
        ISSUER + "/",
        "https://evil.example",
        "https://TOKEN.actions.githubusercontent.com",
        "http://token.actions.githubusercontent.com",
        None,
        ["https://token.actions.githubusercontent.com"],
    ],
)
def test_an_issuer_off_the_allow_list_is_refused_before_any_fetch(
    fetcher: FakeFetcher, iss: object
) -> None:
    token = unsigned({"alg": "RS256", "kid": "k1"}, claims(iss=iss), b"signature")
    assert refusal(verifier(fetcher), token) is RefusalCode.ISSUER_NOT_ALLOWED
    assert fetcher.calls == []


# -- the key and the signature ---------------------------------------------------------------


def test_an_unknown_kid_is_refused(fetcher: FakeFetcher) -> None:
    assert refusal(verifier(fetcher), sign(kid="nope")) is RefusalCode.UNKNOWN_KEY


def test_keys_that_cannot_be_fetched_are_refused() -> None:
    assert refusal(verifier(FakeFetcher()), sign()) is RefusalCode.KEYS_UNAVAILABLE


@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
def test_a_1024_bit_key_is_refused(fetcher: FakeFetcher) -> None:
    assert refusal(verifier(fetcher), sign(key="weak", bits=1024, kid="weak")) is (
        RefusalCode.WEAK_KEY
    )


def test_a_key_bound_to_another_algorithm_is_refused(fetcher: FakeFetcher) -> None:
    check = verifier(fetcher, algorithms=["RS256", "PS256"])
    assert refusal(check, sign(key="pss", kid="pss")) is RefusalCode.KEY_MISMATCH


def test_a_signature_by_another_key_is_refused(fetcher: FakeFetcher) -> None:
    assert refusal(verifier(fetcher), sign(key="attacker")) is RefusalCode.BAD_SIGNATURE


def test_a_payload_changed_after_signing_is_refused(fetcher: FakeFetcher) -> None:
    head, _, sig = sign().split(".")
    body = b64(json.dumps(claims(sub="repo:evil/repo:ref:refs/heads/main")).encode())
    assert refusal(verifier(fetcher), f"{head}.{body}.{sig}") is RefusalCode.BAD_SIGNATURE


def test_an_empty_signature_is_refused(fetcher: FakeFetcher) -> None:
    token = unsigned({"alg": "RS256", "kid": "k1"}, claims())
    assert refusal(verifier(fetcher), token) is RefusalCode.BAD_SIGNATURE


# -- the claims ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "aud",
    [
        None,
        "https://other.example",
        AUDIENCE + "/",
        ["https://other.example"],
        [],
        [AUDIENCE, 5],
        5,
    ],
)
def test_an_audience_that_is_not_ours_is_refused(fetcher: FakeFetcher, aud: object) -> None:
    payload = claims(aud=aud) if aud is not None else claims(aud=None)
    assert refusal(verifier(fetcher), sign(payload)) is RefusalCode.AUDIENCE_MISMATCH


@pytest.mark.parametrize(
    "changes",
    [
        {"exp": None},
        {"iat": None},
        {"exp": "soon"},
        {"iat": 1.5},
        {"iat": True},
        {"nbf": "now"},
        {"sub": None},
        {"sub": ""},
        {"sub": 5},
        {"jti": None},
        {"jti": ""},
        {"jti": 5},
        {"jti": "j" * 256},
    ],
)
def test_a_missing_or_mistyped_claim_is_refused(
    fetcher: FakeFetcher, changes: dict[str, object]
) -> None:
    payload = claims(**changes)
    assert refusal(verifier(fetcher), sign(payload)) is RefusalCode.CLAIM_MISSING


def test_expiry_allows_60_seconds_of_skew(fetcher: FakeFetcher) -> None:
    check = verifier(fetcher)
    token = sign(claims(iat=T - 300, nbf=T - 300, exp=T - 59))
    assert check.verify_id_token(token, now=NOW)
    assert refusal(check, sign(claims(iat=T - 300, nbf=T - 300, exp=T - 60))) is (
        RefusalCode.EXPIRED
    )


def test_not_before_and_issued_at_allow_60_seconds_of_skew(fetcher: FakeFetcher) -> None:
    check = verifier(fetcher)
    assert check.verify_id_token(sign(claims(nbf=T + 60, iat=T + 60, exp=T + 400)), now=NOW)
    assert refusal(check, sign(claims(nbf=T + 61, iat=T, exp=T + 400))) is (
        RefusalCode.NOT_YET_VALID
    )
    assert refusal(check, sign(claims(nbf=None, iat=T + 61, exp=T + 400))) is (
        RefusalCode.NOT_YET_VALID
    )


def test_a_token_older_than_the_maximum_age_is_refused(fetcher: FakeFetcher) -> None:
    check = verifier(fetcher, max_token_age_seconds=600)
    assert check.verify_id_token(sign(claims(iat=T - 660, nbf=None, exp=T + 60)), now=NOW)
    assert refusal(check, sign(claims(iat=T - 661, nbf=None, exp=T + 60))) is RefusalCode.TOO_OLD


def test_a_lifetime_over_a_day_is_refused(fetcher: FakeFetcher) -> None:
    token = sign(claims(exp=T - 30 + 86401))
    assert refusal(verifier(fetcher), token) is RefusalCode.LIFETIME_TOO_LONG


def test_refusals_come_in_the_documented_order(fetcher: FakeFetcher) -> None:
    check = verifier(fetcher)
    everything_wrong = claims(iss="https://evil.example", aud="x", exp=T - 999, jti=None)
    assert refusal(check, sign(everything_wrong, kid=None)) is RefusalCode.MISSING_KEY_ID
    assert refusal(check, sign(everything_wrong)) is RefusalCode.ISSUER_NOT_ALLOWED
    late = claims(aud="x", exp=T - 999, jti=None)
    assert refusal(check, sign(late, key="attacker")) is RefusalCode.BAD_SIGNATURE
    assert refusal(check, sign(late)) is RefusalCode.AUDIENCE_MISMATCH
    assert refusal(check, sign(claims(exp=T - 999, jti=None))) is RefusalCode.EXPIRED
    assert refusal(check, sign(claims(jti=None))) is RefusalCode.CLAIM_MISSING


def test_every_code_is_stable_text() -> None:
    assert {code.value for code in RefusalCode} == {
        "disabled",
        "token_too_large",
        "malformed",
        "algorithm_not_allowed",
        "header_not_allowed",
        "missing_key_id",
        "issuer_not_allowed",
        "unknown_key",
        "keys_unavailable",
        "key_mismatch",
        "weak_key",
        "bad_signature",
        "audience_mismatch",
        "claim_missing",
        "expired",
        "not_yet_valid",
        "too_old",
        "lifetime_too_long",
    }
