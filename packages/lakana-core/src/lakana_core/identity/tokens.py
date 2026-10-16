"""Credential generation, hashing, and parsing — pure functions, no database.

SHA-256, not argon2id: the secret already carries 256 bits of real
randomness, so argon2id's deliberate slowness (which defends against
human-chosen, low-entropy passwords) buys nothing here, while a PAT is
verified on every authenticated request, where a slow hash costs real
latency for no corresponding security benefit. `verify_secret` compares via
`hmac.compare_digest`, not `==`, so the comparison itself cannot leak
timing information about how much of the secret matched.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratedToken:
    """A freshly generated credential, before it is stored anywhere.

    Parameters
    ----------
    key_id
        The public lookup key — safe to store unhashed and indexed.
    secret
        The high-entropy secret half. Never stored; only `secret_hash` is.
    secret_hash
        `hash_secret(secret)`, what actually gets persisted.
    full_token
        `<prefix>_<key_id>_<secret>`, the value shown to the caller exactly
        once.
    """

    key_id: str
    secret: str
    secret_hash: str
    full_token: str


def generate_token(prefix: str) -> GeneratedToken:
    """Generate a new credential.

    Parameters
    ----------
    prefix
        The wire-format prefix, e.g. `"lakana_pat"` or `"lakana_task"`.

    Returns
    -------
    GeneratedToken
        A freshly random key_id and secret, with the secret's hash already
        computed.
    """
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return GeneratedToken(
        key_id=key_id,
        secret=secret,
        secret_hash=hash_secret(secret),
        full_token=f"{prefix}_{key_id}_{secret}",
    )


def hash_secret(secret: str) -> str:
    """Hash a secret for storage.

    Parameters
    ----------
    secret
        The plaintext secret.

    Returns
    -------
    str
        The secret's SHA-256 digest, as a 64-character hex string.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_secret(secret: str, secret_hash: str) -> bool:
    """Check a presented secret against a stored hash, in constant time.

    Parameters
    ----------
    secret
        The plaintext secret presented by the caller.
    secret_hash
        The hash stored for the credential the caller claims to hold.

    Returns
    -------
    bool
        `True` if `secret` hashes to `secret_hash`.
    """
    return hmac.compare_digest(hash_secret(secret), secret_hash)


def parse_token(raw: str, prefix: str) -> tuple[str, str] | None:
    """Split a full token into its key_id and secret.

    Parameters
    ----------
    raw
        The full token value, e.g. `"lakana_pat_abc123_therestofit"`.
    prefix
        The expected wire-format prefix, e.g. `"lakana_pat"`.

    Returns
    -------
    tuple of (str, str), or None
        `(key_id, secret)` if `raw` matches `<prefix>_<key_id>_<secret>`
        (`key_id` itself never contains an underscore, so the first
        underscore after the prefix unambiguously ends it; `secret` is
        everything after that, underscores included — `token_urlsafe`'s
        alphabet contains `_`). `None` if `raw` does not match at all.
    """
    match = re.match(rf"^{re.escape(prefix)}_([^_]+)_(.+)$", raw)
    return (match.group(1), match.group(2)) if match else None
