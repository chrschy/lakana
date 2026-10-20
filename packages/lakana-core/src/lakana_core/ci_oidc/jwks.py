"""Each configured issuer's signing keys: found by discovery, cached, refreshed with restraint.

- Only an issuer in `LAKANA_CI_OIDC__ISSUERS` is ever fetched; the verifier checks the token's
  issuer before it asks for a key, so a token cannot make the server reach any other host.
- The discovery document comes from the issuer's own host and must name the issuer exactly; the
  ``jwks_uri`` it names must be ``https`` on the issuer's host or on a host of
  `LAKANA_CI_OIDC__ALLOW_HOSTS`, and the fetcher (`lakana_core.net.https_json`) checks it again.
- Keys are fresh for `FRESH_SECONDS`. A key id that is not in the set refreshes it, but at most
  once per `REFRESH_INTERVAL_SECONDS` per issuer, whatever the reason, so random key ids cannot
  turn the server into a request generator.
- When a refresh fails, the last good keys stay usable for `STALE_SECONDS` past their freshness;
  after that every lookup fails closed.
- Only RSA signing keys are kept: an entry with another key type, ``use`` other than ``sig``,
  a private part or a malformed modulus is left out. Two keys with one ``kid`` make the whole set
  unusable. The key size is checked when a key is used (`SigningKey.bits`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from jwt.exceptions import PyJWTError

from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_core.ci_oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.net.https_json import FetchFailed, fetch_json

FRESH_SECONDS = 3600.0
"""How long fetched keys are used without asking the issuer again."""

REFRESH_INTERVAL_SECONDS = 60.0
"""The least time between two fetches of one issuer's keys."""

STALE_SECONDS = 86400.0
"""How long past their freshness the last good keys stay usable while refreshes fail."""

MAX_RSA_BITS = 8192
"""The largest RSA key kept; GitHub and GitLab use 2048 bits."""

DISCOVERY_PATH = "/.well-known/openid-configuration"

Fetcher = Callable[[str, Collection[str]], object]
"""Fetch a JSON document: the URL and the host names it may be on, to the parsed value."""

_log = logging.getLogger(__name__)


class _KeySetInvalid(Exception):
    """A discovery document or key set cannot be used.

    Parameters
    ----------
    reason
        A fixed description.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SigningKey:
    """One of an issuer's public signing keys.

    Parameters
    ----------
    kid
        Its key id.
    public_key
        The RSA public key.
    alg
        The algorithm the issuer bound it to, or `None` when the JWK names none.
    """

    kid: str
    public_key: rsa.RSAPublicKey
    alg: str | None

    @property
    def bits(self) -> int:
        """The key size.

        Returns
        -------
        int
            The modulus length in bits.
        """
        return self.public_key.key_size


@dataclass
class _Issuer:
    """The cache state of one issuer.

    Parameters
    ----------
    url
        The issuer.
    """

    url: str
    # (keys by kid, monotonic time fetched); replaced as a whole, so a reader needs no lock.
    snapshot: tuple[Mapping[str, SigningKey], float] | None = None
    attempted_at: float | None = None
    refreshing: threading.Lock = field(default_factory=threading.Lock)


def _parse_key(entry: object) -> SigningKey | None:
    """Turn one JWK into a signing key, or leave it out.

    Parameters
    ----------
    entry
        One member of the set's ``keys``.

    Returns
    -------
    SigningKey or None
        The key, or `None` for anything but a well-formed public RSA signing key with a key id
        and at most `MAX_RSA_BITS` bits (a larger one only costs time to verify).
    """
    if not isinstance(entry, dict) or entry.get("kty") != "RSA":
        return None
    kid, alg = entry.get("kid"), entry.get("alg")
    if not isinstance(kid, str) or not kid or entry.get("use", "sig") != "sig" or "d" in entry:
        return None
    try:
        public_key = RSAAlgorithm.from_jwk(entry)
    except (PyJWTError, ValueError, TypeError):
        return None
    if not isinstance(public_key, rsa.RSAPublicKey):  # pragma: no cover - "d" is refused above
        return None
    if public_key.key_size > MAX_RSA_BITS:
        return None
    return SigningKey(kid=kid, public_key=public_key, alg=alg if isinstance(alg, str) else None)


def _parse_key_set(document: object) -> dict[str, SigningKey]:
    """Read the usable keys of a key set.

    Parameters
    ----------
    document
        The fetched key set.

    Returns
    -------
    dict[str, SigningKey]
        The keys by key id.

    Raises
    ------
    _KeySetInvalid
        If the document has no ``keys`` list, or two keys share a key id.
    """
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise _KeySetInvalid("the key set has no list of keys")
    keys: dict[str, SigningKey] = {}
    for entry in document["keys"]:
        key = _parse_key(entry)
        if key is None:
            continue
        if key.kid in keys:
            raise _KeySetInvalid("two keys of the key set share a key id")
        keys[key.kid] = key
    return keys


class JwksCache:
    """The signing keys of the configured issuers; safe to share between threads.

    Parameters
    ----------
    config
        The operator settings: the issuers, the allowed key hosts, the proxy and the CA bundle.
    fetcher
        Fetches one JSON document; the default is `lakana_core.net.https_json.fetch_json` with
        the operator's proxy and CA bundle.
    clock
        A monotonic clock in seconds.
    """

    def __init__(
        self,
        config: CiOidcConfig,
        *,
        fetcher: Fetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._fetch = fetcher or self._fetch_with_settings
        self._clock = clock
        self._issuers = {url: _Issuer(url) for url in config.issuers}

    def _fetch_with_settings(self, url: str, allowed_hosts: Collection[str]) -> object:
        """Fetch a document with the operator's proxy and CA bundle.

        Parameters
        ----------
        url
            The URL.
        allowed_hosts
            The host names it may be on.

        Returns
        -------
        object
            The parsed document.
        """
        return fetch_json(
            url,
            allowed_hosts=allowed_hosts,
            proxy=self._config.proxy,
            ca_bundle=self._config.ca_bundle,
        )

    def _load(self, issuer: str) -> dict[str, SigningKey]:
        """Fetch an issuer's discovery document and then its key set.

        Parameters
        ----------
        issuer
            The issuer URL.

        Returns
        -------
        dict[str, SigningKey]
            The keys by key id.

        Raises
        ------
        _KeySetInvalid
            If the discovery document names another issuer or a ``jwks_uri`` that is not
            ``https`` on an allowed host, or the key set is unusable.
        """
        own_host = urlsplit(issuer).hostname or ""
        document = self._fetch(issuer + DISCOVERY_PATH, frozenset({own_host}))
        if not isinstance(document, dict) or document.get("issuer") != issuer:
            raise _KeySetInvalid("the discovery document names another issuer")
        jwks_uri = document.get("jwks_uri")
        allowed = frozenset({own_host, *self._config.allow_hosts})
        scheme, netloc, host = "", "", ""
        if isinstance(jwks_uri, str):
            try:
                parts = urlsplit(jwks_uri)
                scheme, netloc, host = parts.scheme, parts.netloc, parts.hostname or ""
            except ValueError:
                pass
        if scheme != "https" or "@" in netloc or host not in allowed:
            raise _KeySetInvalid(
                "the discovery document's jwks_uri is not https on the issuer's host or a host "
                "of LAKANA_CI_OIDC__ALLOW_HOSTS"
            )
        return _parse_key_set(self._fetch(str(jwks_uri), allowed))

    def _refresh(self, state: _Issuer, now: float) -> None:
        """Fetch the keys again, keeping the last good ones when that fails.

        Parameters
        ----------
        state
            The issuer's cache state; its refresh lock is held.
        now
            The clock's time.
        """
        state.attempted_at = now
        reason = ""
        try:
            state.snapshot = (self._load(state.url), now)
        except (FetchFailed, _KeySetInvalid) as failed:
            reason = failed.reason
        if reason:
            _log.warning("could not fetch the CI OIDC keys of %s: %s", state.url, reason)

    @staticmethod
    def _fresh_key(state: _Issuer, kid: str, now: float) -> SigningKey | None:
        """Return a key from fresh keys only.

        Parameters
        ----------
        state
            The issuer's cache state.
        kid
            The key id.
        now
            The clock's time.

        Returns
        -------
        SigningKey or None
            The key, or `None` when the keys are not fresh or lack it.
        """
        snapshot = state.snapshot
        if snapshot is None or now - snapshot[1] >= FRESH_SECONDS:
            return None
        return snapshot[0].get(kid)

    def signing_key(self, issuer: str, kid: str) -> SigningKey:
        """Return an issuer's key by its key id.

        Parameters
        ----------
        issuer
            The issuer URL, exactly as configured.
        kid
            The key id from the token's header.

        Returns
        -------
        SigningKey
            The key.

        Raises
        ------
        IdTokenRefused
            `ISSUER_NOT_ALLOWED` for an issuer that is not configured (nothing is fetched),
            `KEYS_UNAVAILABLE` when there are no usable keys, `UNKNOWN_KEY` when the keys lack
            this key id.
        """
        state = self._issuers.get(issuer)
        if state is None:
            raise IdTokenRefused(RefusalCode.ISSUER_NOT_ALLOWED)
        key = self._fresh_key(state, kid, self._clock())
        if key is not None:
            return key
        with state.refreshing:
            now = self._clock()
            key = self._fresh_key(state, kid, now)
            if key is not None:
                return key
            if state.attempted_at is None or now - state.attempted_at >= REFRESH_INTERVAL_SECONDS:
                self._refresh(state, now)
        snapshot = state.snapshot
        if snapshot is None or now - snapshot[1] >= FRESH_SECONDS + STALE_SECONDS:
            raise IdTokenRefused(RefusalCode.KEYS_UNAVAILABLE)
        key = snapshot[0].get(kid)
        if key is None:
            raise IdTokenRefused(RefusalCode.UNKNOWN_KEY)
        return key
