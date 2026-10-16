"""Tests for KeyProvider/EnvKeyProvider."""

import base64

import pytest

from lakana_core.errors import LakanaError
from lakana_core.secrets.keys import EnvKeyProvider, UnwrapError


def _b64_key() -> str:
    """Generate a valid base64-encoded 32-byte master key for tests.

    Returns
    -------
    str
        A base64-encoded 32-byte key.
    """
    return base64.b64encode(b"0" * 32).decode()


def test_wrap_then_unwrap_recovers_the_original_data_key() -> None:
    """A data key wrapped and then unwrapped comes back unchanged."""
    provider = EnvKeyProvider(_b64_key())
    data_key = b"\x01" * 32

    wrapped = provider.wrap(data_key)
    assert wrapped != data_key
    assert provider.unwrap(wrapped) == data_key


def test_wrap_raises_when_no_master_key_is_configured() -> None:
    """An unset master key fails the operation, not startup."""
    provider = EnvKeyProvider(None)
    with pytest.raises(LakanaError, match="master key"):
        provider.wrap(b"\x01" * 32)


def test_unwrap_raises_when_no_master_key_is_configured() -> None:
    """Same as wrap: unwrap fails cleanly, not with an unrelated crash."""
    provider = EnvKeyProvider(None)
    with pytest.raises(LakanaError, match="master key"):
        provider.unwrap(b"\x01" * 48)


def test_unwrap_rejects_a_wrapped_key_from_a_different_master_key() -> None:
    """A wrapped key cannot be unwrapped under a different master key."""
    wrapped = EnvKeyProvider(_b64_key()).wrap(b"\x01" * 32)
    other_key = base64.b64encode(b"1" * 32).decode()

    with pytest.raises(LakanaError, match="decrypt"):
        EnvKeyProvider(other_key).unwrap(wrapped)


def test_a_malformed_master_key_raises_a_clear_error() -> None:
    """A master key that isn't valid base64, or isn't 32 bytes once decoded, is rejected."""
    with pytest.raises(LakanaError, match="master key"):
        EnvKeyProvider("not valid base64!!!").wrap(b"\x01" * 32)
    with pytest.raises(LakanaError, match="master key"):
        EnvKeyProvider(base64.b64encode(b"too short").decode()).wrap(b"\x01" * 32)


def test_key_id_names_the_provider_and_the_key_without_revealing_it() -> None:
    """The identifier is `env:` plus a short keyed fingerprint: stable, and not the key."""
    key = _b64_key()
    key_id = EnvKeyProvider(key).key_id

    assert key_id == EnvKeyProvider(key).key_id
    assert key_id.startswith("env:")
    assert len(key_id) == len("env:") + 16
    raw_hex = (b"0" * 32).hex()
    assert key not in key_id
    assert raw_hex[:16] not in key_id
    assert key_id != EnvKeyProvider(base64.b64encode(b"1" * 32).decode()).key_id


def test_key_id_needs_a_usable_master_key() -> None:
    """Asking a provider with no (or a malformed) key for its identifier is the usual error."""
    with pytest.raises(LakanaError, match="master key"):
        _ = EnvKeyProvider(None).key_id


def test_wrap_binds_the_data_key_to_the_associated_data() -> None:
    """A wrapped key unwraps only with the associated data it was wrapped with."""
    provider = EnvKeyProvider(_b64_key())
    wrapped = provider.wrap(b"\x01" * 32, b"row-a")

    assert provider.unwrap(wrapped, b"row-a") == b"\x01" * 32
    for wrong in (b"row-b", b"", None):
        with pytest.raises(UnwrapError, match="decrypt"):
            provider.unwrap(wrapped, wrong)
    with pytest.raises(UnwrapError):
        provider.unwrap(provider.wrap(b"\x01" * 32), b"row-a")


def test_unwrap_failure_is_a_lakana_error() -> None:
    """The refusal stays a `LakanaError`, so callers that catch that keep working."""
    assert issubclass(UnwrapError, LakanaError)


def test_a_key_provider_refuses_format_1_unless_built_to_accept_it() -> None:
    """The setting is carried by the provider; the default and the abstract default refuse."""
    from lakana_core.secrets.keys import KeyProvider

    assert EnvKeyProvider(_b64_key()).accepts_format_1 is False
    assert EnvKeyProvider(_b64_key(), accept_format_1=True).accepts_format_1 is True
    assert KeyProvider.accepts_format_1.fget(EnvKeyProvider(None)) is False
