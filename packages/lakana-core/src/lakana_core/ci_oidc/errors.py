"""The refusals of a CI ID token, each with a stable code.

The code is for the caller (the exchange endpoint's rate-limited ``OIDC_EXCHANGE_REFUSED`` audit
row, and its answer); the message is for a person and says what to check. Neither ever contains
the token or a value taken from it.
"""

from __future__ import annotations

import enum


class RefusalCode(enum.StrEnum):
    """Why an ID token was refused; the values are stable."""

    DISABLED = "disabled"
    TOKEN_TOO_LARGE = "token_too_large"  # nosec B105 -- a refusal code, not a credential
    MALFORMED = "malformed"
    ALGORITHM_NOT_ALLOWED = "algorithm_not_allowed"
    HEADER_NOT_ALLOWED = "header_not_allowed"
    MISSING_KEY_ID = "missing_key_id"
    ISSUER_NOT_ALLOWED = "issuer_not_allowed"
    UNKNOWN_KEY = "unknown_key"
    KEYS_UNAVAILABLE = "keys_unavailable"
    KEY_MISMATCH = "key_mismatch"
    WEAK_KEY = "weak_key"
    BAD_SIGNATURE = "bad_signature"
    AUDIENCE_MISMATCH = "audience_mismatch"
    CLAIM_MISSING = "claim_missing"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    TOO_OLD = "too_old"
    LIFETIME_TOO_LONG = "lifetime_too_long"


_MESSAGES = {
    RefusalCode.DISABLED: "CI OIDC is off: set LAKANA_CI_OIDC__ISSUERS on the server to use it",
    RefusalCode.TOKEN_TOO_LARGE: "the ID token is larger than 16 KiB",
    RefusalCode.MALFORMED: "the ID token is not a well-formed signed JWT",
    RefusalCode.ALGORITHM_NOT_ALLOWED: (
        "the ID token's signature algorithm is not in LAKANA_CI_OIDC__ALGORITHMS"
    ),
    RefusalCode.HEADER_NOT_ALLOWED: (
        "the ID token's header names a key or a critical extension (jku, x5u, x5c, jwk, crit), "
        "which is never accepted: the keys come from the issuer only"
    ),
    RefusalCode.MISSING_KEY_ID: "the ID token's header has no usable key id (kid)",
    RefusalCode.ISSUER_NOT_ALLOWED: (
        "the ID token's issuer (iss) is not in LAKANA_CI_OIDC__ISSUERS; it must match an entry "
        "exactly"
    ),
    RefusalCode.UNKNOWN_KEY: "the issuer's published keys have no key with the ID token's key id",
    RefusalCode.KEYS_UNAVAILABLE: (
        "the issuer's keys could not be fetched; check the server's outbound https access to "
        "the issuer (LAKANA_CI_OIDC__PROXY, LAKANA_CI_OIDC__CA_BUNDLE, LAKANA_CI_OIDC__ALLOW_HOSTS)"
    ),
    RefusalCode.KEY_MISMATCH: "the issuer's key with this key id is not for this algorithm",
    RefusalCode.WEAK_KEY: "the issuer's key with this key id is an RSA key under 2048 bits",
    RefusalCode.BAD_SIGNATURE: "the ID token's signature does not verify with the issuer's key",
    RefusalCode.AUDIENCE_MISMATCH: (
        "the ID token is not for this server: request it for the audience in "
        "LAKANA_CI_OIDC__AUDIENCE"
    ),
    RefusalCode.CLAIM_MISSING: (
        "the ID token lacks a required claim (sub, jti, exp or iat) or has one of the wrong type"
    ),
    RefusalCode.EXPIRED: "the ID token has expired; request a new one in the CI job",
    RefusalCode.NOT_YET_VALID: (
        "the ID token is not valid yet; check the clocks of the server and the CI runner"
    ),
    RefusalCode.TOO_OLD: (
        "the ID token was issued too long ago (LAKANA_CI_OIDC__MAX_TOKEN_AGE_SECONDS); request "
        "it right before it is used"
    ),
    RefusalCode.LIFETIME_TOO_LONG: "the ID token claims a lifetime longer than 24 hours",
}


class IdTokenRefused(Exception):
    """An ID token was refused.

    Parameters
    ----------
    code
        Why; stable, for audit rows and answers.
    """

    def __init__(self, code: RefusalCode) -> None:
        message = _MESSAGES[code]
        super().__init__(message)
        self.code = code
        self.message = message
