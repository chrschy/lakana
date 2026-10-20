"""Keys, key sets, discovery documents and a fake fetcher for the CI OIDC tests.

No test reaches the network: `FakeFetcher` answers from a dictionary and records every request.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from functools import cache

from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from lakana_core.net.https_json import FetchFailed

ISSUER = "https://token.actions.githubusercontent.com"
ISSUER_HOST = "token.actions.githubusercontent.com"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER}/.well-known/jwks"
AUDIENCE = "https://lakana.example.com"


@cache
def _generated(name: str, bits: int) -> rsa.RSAPrivateKey:
    """Generate a key once per name and size.

    Parameters
    ----------
    name
        Any label.
    bits
        The key size.

    Returns
    -------
    rsa.RSAPrivateKey
        The key.
    """
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def rsa_key(name: str, bits: int = 2048) -> rsa.RSAPrivateKey:
    """Return a private RSA key, the same for the same name and size within a run.

    Parameters
    ----------
    name
        Any label.
    bits
        The key size.

    Returns
    -------
    rsa.RSAPrivateKey
        The key.
    """
    return _generated(name, bits)


def jwk(key: rsa.RSAPrivateKey, kid: str, **extra: object) -> dict[str, object]:
    """Publish a key's public half as a JWK.

    Parameters
    ----------
    key
        The private key.
    kid
        Its key id.
    **extra
        Further members, such as ``alg`` or ``use``.

    Returns
    -------
    dict[str, object]
        The JWK.
    """
    published = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return {**published, "kid": kid, **extra}


def discovery(jwks_uri: str = JWKS_URI, issuer: str = ISSUER) -> dict[str, object]:
    """Build a discovery document.

    Parameters
    ----------
    jwks_uri
        Where it says the keys are.
    issuer
        The issuer it names.

    Returns
    -------
    dict[str, object]
        The document.
    """
    return {"issuer": issuer, "jwks_uri": jwks_uri}


@dataclass
class FakeFetcher:
    """Answer fetches from a dictionary, recording each one.

    Parameters
    ----------
    documents
        URL to document; a `FetchFailed` value is raised instead of returned.
    """

    documents: dict[str, object] = field(default_factory=dict)
    calls: list[tuple[str, frozenset[str]]] = field(default_factory=list)

    def __call__(self, url: str, allowed_hosts: Collection[str]) -> object:
        """Fetch a document.

        Parameters
        ----------
        url
            The URL.
        allowed_hosts
            The hosts the request may reach.

        Returns
        -------
        object
            The document.

        Raises
        ------
        FetchFailed
            When the dictionary says so, or has no document for the URL.
        """
        self.calls.append((url, frozenset(allowed_hosts)))
        document = self.documents.get(url, FetchFailed("not found"))
        if isinstance(document, FetchFailed):
            raise document
        return document

    def publish(self, *keys: object) -> None:
        """Serve a discovery document and a key set with these keys at the issuer.

        Parameters
        ----------
        *keys
            The JWKs (or anything else, to test what the cache leaves out).
        """
        self.documents[DISCOVERY] = discovery()
        self.documents[JWKS_URI] = {"keys": list(keys)}


def b64(data: bytes) -> str:
    """Encode as unpadded base64url.

    Parameters
    ----------
    data
        The bytes.

    Returns
    -------
    str
        The text.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unsigned(header: Mapping[str, object], payload: Mapping[str, object], sig: bytes = b"") -> str:
    """Assemble a JWT by hand, with any header and signature.

    Parameters
    ----------
    header
        The header.
    payload
        The claims.
    sig
        The signature bytes.

    Returns
    -------
    str
        The compact token.
    """
    head = b64(json.dumps(dict(header)).encode())
    body = b64(json.dumps(dict(payload)).encode())
    return f"{head}.{body}.{b64(sig)}"
