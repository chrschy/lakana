"""Verify a CI provider's OIDC ID token, in a fixed order.

The order matters: everything that can be decided from the token alone is decided first, and
the token's issuer is checked against `LAKANA_CI_OIDC__ISSUERS` **before** any network call, so
an attacker's token can make the server fetch nothing but a configured issuer's keys (and those
at most once a minute, `lakana_core.ci_oidc.jwks`).

1. At most `MAX_TOKEN_BYTES`, three base64url segments, a JSON object as header and payload,
   no member twice (so the claims checked here are the ones that were signed).
2. The header's ``alg`` is in `LAKANA_CI_OIDC__ALGORITHMS` (never ``none`` or HMAC).
3. A header that supplies a key or a critical extension (``jku``, ``x5u``, ``x5c``, ``jwk``,
   ``crit``) is refused: the keys come from the issuer only.
4. A ``kid`` is present.
5. The unverified ``iss`` is exactly one of the configured issuers (no normalisation: a trailing
   slash is another issuer). The signature, checked next, then binds it.
6. The key with that ``kid`` from the issuer's key set; it must be RSA, of at least 2048 bits and
   not bound to another algorithm.
7. The signature (PyJWT, with this one algorithm and this one key).
8. ``aud`` is, or contains, `LAKANA_CI_OIDC__AUDIENCE`.
9. ``exp``, ``nbf`` and ``iat`` with `CLOCK_SKEW_SECONDS` of skew, a maximum age since ``iat``
   (`LAKANA_CI_OIDC__MAX_TOKEN_AGE_SECONDS`) and a lifetime of at most a day.
10. ``sub`` and ``jti`` are present.

Single use of the ``jti`` is the caller's: it needs the database. `VerifiedIdToken` carries the
``jti`` and the expiry, so the caller can record the one until the other.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from jwt.api_jws import PyJWS
from jwt.exceptions import PyJWTError

from lakana_core.ci_oidc.config import SUPPORTED_ALGORITHMS, CiOidcConfig
from lakana_core.ci_oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.ci_oidc.jwks import JwksCache

MAX_TOKEN_BYTES = 16 * 1024
"""The largest ID token looked at; GitHub's and GitLab's are about 1 to 2 KiB."""

CLOCK_SKEW_SECONDS = 60
"""How far the server's clock and the issuer's may disagree."""

MAX_LIFETIME_SECONDS = 86400
"""The longest lifetime (``exp`` minus ``iat``) a token may claim."""

MIN_RSA_BITS = 2048

_MAX_KID = 256
_MAX_JTI = 255
_COMPACT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_KEY_HEADERS = ("jku", "x5u", "x5c", "jwk", "crit")
_JWS = PyJWS()


@dataclass(frozen=True)
class VerifiedIdToken:
    """An ID token whose signature, issuer, audience and times have been checked.

    Parameters
    ----------
    claims
        Every claim of the token, for the caller's trust rules.
    issuer
        The issuer, exactly as configured.
    subject
        The ``sub`` claim.
    jti
        The token's id; the caller must accept it once only.
    expires_at
        When the token expires (``exp``); the caller keeps the ``jti`` until then.
    """

    claims: Mapping[str, object]
    issuer: str
    subject: str
    jti: str
    expires_at: datetime


def _refuse_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, refusing a member that appears twice.

    Parameters
    ----------
    pairs
        The members in order.

    Returns
    -------
    dict[str, object]
        The object.

    Raises
    ------
    ValueError
        If a name repeats: parsers disagree on which one wins.
    """
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("a member appears twice")
    return result


def _segment(text: str) -> dict[str, object] | None:
    """Decode one base64url segment as a JSON object.

    Parameters
    ----------
    text
        The segment, without padding.

    Returns
    -------
    dict[str, object] or None
        The object, or `None` when the segment is not a JSON object in UTF-8 without repeated
        members (or is nested too deeply to parse).
    """
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_refuse_duplicates)
    except (ValueError, RecursionError):
        # RecursionError: a deeply nested array or object.
        return None
    return value if isinstance(value, dict) else None


class IdTokenVerifier:
    """Check CI ID tokens against the operator's settings; safe to share between threads.

    Parameters
    ----------
    config
        The operator settings (`LAKANA_CI_OIDC__*`).
    keys
        The issuers' key cache; by default one built from `config` that fetches over https.
    """

    def __init__(self, config: CiOidcConfig, keys: JwksCache | None = None) -> None:
        self._config = config
        self._keys = keys or JwksCache(config)
        self._issuers = frozenset(config.issuers)
        self._algorithms = frozenset(config.algorithms)

    @property
    def issuers(self) -> frozenset[str]:
        """The configured issuers (`LAKANA_CI_OIDC__ISSUERS`, resolved).

        Returns
        -------
        frozenset of str
            The issuer URLs; empty when CI OIDC is off.
        """
        return self._issuers

    def _header(self, token: str) -> tuple[str, str, dict[str, object]]:
        """Check the token's shape and header (steps 1 to 4).

        Parameters
        ----------
        token
            The token.

        Returns
        -------
        tuple[str, str, dict[str, object]]
            The algorithm, the key id and the unverified payload.

        Raises
        ------
        IdTokenRefused
            `TOKEN_TOO_LARGE`, `MALFORMED`, `ALGORITHM_NOT_ALLOWED`, `HEADER_NOT_ALLOWED` or
            `MISSING_KEY_ID`.
        """
        if not isinstance(token, str):
            raise IdTokenRefused(RefusalCode.MALFORMED)
        if len(token) > MAX_TOKEN_BYTES:
            raise IdTokenRefused(RefusalCode.TOKEN_TOO_LARGE)
        if not _COMPACT.fullmatch(token):
            raise IdTokenRefused(RefusalCode.MALFORMED)
        head, body, _ = token.split(".")
        header = _segment(head)
        payload = _segment(body)
        if header is None or payload is None:
            raise IdTokenRefused(RefusalCode.MALFORMED)
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in self._algorithms:
            raise IdTokenRefused(RefusalCode.ALGORITHM_NOT_ALLOWED)
        if any(name in header for name in _KEY_HEADERS):
            raise IdTokenRefused(RefusalCode.HEADER_NOT_ALLOWED)
        kid = header.get("kid")
        if not isinstance(kid, str) or not 0 < len(kid) <= _MAX_KID:
            raise IdTokenRefused(RefusalCode.MISSING_KEY_ID)
        return alg, kid, payload

    def _check_signature(self, token: str, alg: str, issuer: str, kid: str) -> None:
        """Find the key and verify the signature (steps 6 and 7).

        Parameters
        ----------
        token
            The token.
        alg
            Its allowed algorithm.
        issuer
            Its configured issuer.
        kid
            Its key id.

        Raises
        ------
        IdTokenRefused
            `UNKNOWN_KEY`, `KEYS_UNAVAILABLE`, `KEY_MISMATCH`, `WEAK_KEY` or `BAD_SIGNATURE`.
        """
        key = self._keys.signing_key(issuer, kid)
        # Only RSA algorithms ever reach PyJWT, whatever the settings say: an HMAC "verified"
        # with a public key is the classic forgery.
        if alg not in SUPPORTED_ALGORITHMS or (key.alg is not None and key.alg != alg):
            raise IdTokenRefused(RefusalCode.KEY_MISMATCH)
        if key.bits < MIN_RSA_BITS:
            raise IdTokenRefused(RefusalCode.WEAK_KEY)
        verified = True
        try:
            _JWS.decode_complete(token, key=key.public_key, algorithms=[alg])
        except PyJWTError:
            verified = False
        if not verified:
            raise IdTokenRefused(RefusalCode.BAD_SIGNATURE)

    def _check_audience(self, payload: Mapping[str, object]) -> None:
        """Require our audience (step 8).

        Parameters
        ----------
        payload
            The claims.

        Raises
        ------
        IdTokenRefused
            `AUDIENCE_MISMATCH`.
        """
        aud = payload.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if not all(isinstance(item, str) for item in audiences) or (
            self._config.audience not in audiences
        ):
            raise IdTokenRefused(RefusalCode.AUDIENCE_MISMATCH)

    def _check_times(self, payload: Mapping[str, object], now: float) -> int:
        """Check ``exp``, ``nbf`` and ``iat`` (step 9).

        Parameters
        ----------
        payload
            The claims.
        now
            The time of the check, in seconds since the epoch.

        Returns
        -------
        int
            The expiry.

        Raises
        ------
        IdTokenRefused
            `CLAIM_MISSING`, `EXPIRED`, `NOT_YET_VALID`, `TOO_OLD` or `LIFETIME_TOO_LONG`.
        """
        exp, iat, nbf = payload.get("exp"), payload.get("iat"), payload.get("nbf", 0)
        if not (isinstance(exp, int) and isinstance(iat, int) and isinstance(nbf, int)) or any(
            isinstance(value, bool) for value in (exp, iat, nbf)
        ):
            raise IdTokenRefused(RefusalCode.CLAIM_MISSING)
        if exp <= now - CLOCK_SKEW_SECONDS:
            raise IdTokenRefused(RefusalCode.EXPIRED)
        if nbf > now + CLOCK_SKEW_SECONDS or iat > now + CLOCK_SKEW_SECONDS:
            raise IdTokenRefused(RefusalCode.NOT_YET_VALID)
        if now - iat > self._config.max_token_age_seconds + CLOCK_SKEW_SECONDS:
            raise IdTokenRefused(RefusalCode.TOO_OLD)
        if exp - iat > MAX_LIFETIME_SECONDS:
            raise IdTokenRefused(RefusalCode.LIFETIME_TOO_LONG)
        return exp

    def verify_id_token(self, token: str, *, now: datetime) -> VerifiedIdToken:
        """Verify an ID token.

        Parameters
        ----------
        token
            The compact JWT, as the CI provider issued it.
        now
            The time of the check, with a time zone.

        Returns
        -------
        VerifiedIdToken
            The checked token. Its ``jti`` has not been checked for reuse.

        Raises
        ------
        IdTokenRefused
            With the code of the first check that failed, in the order of this module's
            docstring; `DISABLED` when no issuer is configured.
        ValueError
            If `now` has no time zone.
        """
        if now.tzinfo is None:
            raise ValueError("now must carry a time zone")
        if not self._issuers:
            raise IdTokenRefused(RefusalCode.DISABLED)
        alg, kid, payload = self._header(token)
        issuer = payload.get("iss")
        if not isinstance(issuer, str) or issuer not in self._issuers:
            raise IdTokenRefused(RefusalCode.ISSUER_NOT_ALLOWED)
        self._check_signature(token, alg, issuer, kid)
        self._check_audience(payload)
        exp = self._check_times(payload, now.timestamp())
        subject, jti = payload.get("sub"), payload.get("jti")
        if not isinstance(subject, str) or not subject:
            raise IdTokenRefused(RefusalCode.CLAIM_MISSING)
        if not isinstance(jti, str) or not 0 < len(jti) <= _MAX_JTI:
            raise IdTokenRefused(RefusalCode.CLAIM_MISSING)
        return VerifiedIdToken(
            claims=payload,
            issuer=issuer,
            subject=subject,
            jti=jti,
            expires_at=datetime.fromtimestamp(exp, UTC),
        )
