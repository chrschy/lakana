"""Tests for the token generation, hashing, and parsing primitives."""

from lakana_core.identity.tokens import generate_token, hash_secret, parse_token, verify_secret


def test_generate_token_produces_the_documented_shape() -> None:
    """The full token is `<prefix>_<key_id>_<secret>`, and the hash matches the secret."""
    token = generate_token("lakana_pat")
    assert token.full_token == f"lakana_pat_{token.key_id}_{token.secret}"
    assert "_" not in token.key_id
    assert verify_secret(token.secret, token.secret_hash)


def test_generate_token_produces_distinct_values_each_call() -> None:
    """Two calls never collide — both the key_id and the secret are freshly random."""
    a = generate_token("lakana_pat")
    b = generate_token("lakana_pat")
    assert a.key_id != b.key_id
    assert a.secret != b.secret


def test_verify_secret_rejects_the_wrong_secret() -> None:
    """A secret that doesn't hash to the stored value fails verification."""
    token = generate_token("lakana_pat")
    assert not verify_secret("wrong-secret", token.secret_hash)


def test_hash_secret_is_deterministic() -> None:
    """The same secret always hashes to the same value, so lookup-by-hash is possible."""
    assert hash_secret("a-secret") == hash_secret("a-secret")


def test_parse_token_splits_key_id_and_secret() -> None:
    """A full token parses back into exactly the (key_id, secret) pair it was built from."""
    token = generate_token("lakana_pat")
    parsed = parse_token(token.full_token, "lakana_pat")
    assert parsed == (token.key_id, token.secret)


def test_parse_token_handles_a_secret_containing_underscores() -> None:
    """token_urlsafe's alphabet includes '_'; parsing must not split on the wrong one."""
    parsed = parse_token("lakana_pat_abc123_se_cr_et", "lakana_pat")
    assert parsed == ("abc123", "se_cr_et")


def test_parse_token_rejects_a_different_prefix() -> None:
    """A task-token-shaped string does not parse as a PAT, and vice versa."""
    assert parse_token("lakana_task_abc123_secret", "lakana_pat") is None


def test_parse_token_rejects_garbage() -> None:
    """Anything not matching `<prefix>_<key_id>_<secret>` parses to None, not an exception."""
    assert parse_token("not-a-token-at-all", "lakana_pat") is None
