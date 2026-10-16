"""Tests for the secrets' stored format: encryption, the row binding and the legacy read."""

import base64
import itertools
import logging
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from core_test_helpers import LEGACY_SECRET, LEGACY_VALUES
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from lakana_core.errors import LakanaError
from lakana_core.models import Secret
from lakana_core.secrets.crypto import (
    ACCEPT_FORMAT_1_WARNING,
    CURRENT_FORMAT,
    EncryptedSecret,
    LegacyFormatRefusedError,
    SecretBinding,
    SecretFormatError,
    associated_data,
    decrypt_secret_row,
    decrypt_secret_values,
    encrypt_secret_values,
)
from lakana_core.secrets.keys import EnvKeyProvider, UnwrapError

_ENV = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_BINDING = SecretBinding(secret_id=_ID, environment_id=_ENV, name="s3-creds")
_REFUSED = (InvalidTag, UnwrapError)
_UNDECRYPTABLE = (
    "Secret 's3-creds' in environment 'dev' could not be decrypted. If "
    "LAKANA_SECRETS__MASTER_KEY was changed, put back the key the secret was stored under. "
    "Otherwise restore it from a backup, or set it again with `lakana secret rotate s3-creds "
    "--env dev --values-stdin`."
)


def _provider() -> EnvKeyProvider:
    """Create a KeyProvider backed by a fixed, valid master key.

    Returns
    -------
    EnvKeyProvider
        A key provider for testing.
    """
    return EnvKeyProvider(base64.b64encode(b"0" * 32).decode())


def _row(binding: SecretBinding, encrypted: EncryptedSecret) -> Secret:
    """Build a `Secret` row (not persisted) holding `encrypted` under `binding`.

    Parameters
    ----------
    binding
        The row's id, environment and name.
    encrypted
        The encrypted triple, format version and key id to store.

    Returns
    -------
    Secret
        The row.
    """
    return Secret(
        id=binding.secret_id,
        environment_id=binding.environment_id,
        name=binding.name,
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        wrapped_data_key=encrypted.wrapped_data_key,
        format_version=encrypted.format_version,
        key_id=encrypted.key_id,
        created_by=uuid.uuid4(),
        created_at=datetime.now(UTC),
    )


def test_encrypt_then_decrypt_recovers_the_original_values() -> None:
    """A key-value bag round-trips exactly, as the current format, naming the key."""
    provider = _provider()
    values = {"access_key_id": "AKIA_EXAMPLE", "secret_access_key": "super-secret-value"}

    encrypted = encrypt_secret_values(values, provider, _BINDING)

    assert isinstance(encrypted, EncryptedSecret)
    assert encrypted.ciphertext != b""
    assert encrypted.format_version == CURRENT_FORMAT == 2
    assert encrypted.key_id == provider.key_id
    assert decrypt_secret_values(encrypted, provider, _BINDING) == values


def test_encrypting_the_same_values_twice_produces_different_ciphertext() -> None:
    """Fresh nonces and a fresh data key mean no two encryptions look alike."""
    provider = _provider()
    values = {"password": "hunter2"}  # nosec B105 -- a test fixture value, not a real credential

    first = encrypt_secret_values(values, provider, _BINDING)
    second = encrypt_secret_values(values, provider, _BINDING)

    assert first.ciphertext != second.ciphertext
    assert first.wrapped_data_key != second.wrapped_data_key


def test_tampering_with_the_ciphertext_is_detected() -> None:
    """A single flipped byte in the ciphertext fails decryption rather than returning garbage."""
    provider = _provider()
    encrypted = encrypt_secret_values({"k": "v"}, provider, _BINDING)
    tampered = replace(
        encrypted, ciphertext=bytes([encrypted.ciphertext[0] ^ 1]) + encrypted.ciphertext[1:]
    )

    with pytest.raises(InvalidTag):
        decrypt_secret_values(tampered, provider, _BINDING)


def test_decrypting_under_a_different_master_key_fails() -> None:
    """A secret encrypted under one master key cannot be read under another."""
    encrypted = encrypt_secret_values({"k": "v"}, _provider(), _BINDING)
    other_provider = EnvKeyProvider(base64.b64encode(b"1" * 32).decode())

    with pytest.raises(LakanaError, match="decrypt"):
        decrypt_secret_values(encrypted, other_provider, _BINDING)


@pytest.mark.parametrize(
    "other",
    [
        SecretBinding(uuid.uuid4(), _ENV, "s3-creds"),
        SecretBinding(uuid.uuid4(), uuid.uuid4(), "s3-creds"),
        SecretBinding(_ID, uuid.uuid4(), "s3-creds"),
        SecretBinding(_ID, _ENV, "other-name"),
    ],
    ids=["another secret", "another environment", "same id other environment", "another name"],
)
def test_a_secret_copied_onto_another_row_does_not_decrypt(other: SecretBinding) -> None:
    """The whole triple, moved to a row that differs in any bound field, is refused."""
    provider = _provider()
    encrypted = encrypt_secret_values({"k": "v"}, provider, _BINDING)

    with pytest.raises(_REFUSED):
        decrypt_secret_values(encrypted, provider, other)


def test_a_swapped_key_id_does_not_decrypt() -> None:
    """The key id is bound: the same bytes under another key id fail."""
    provider = _provider()
    encrypted = encrypt_secret_values({"k": "v"}, provider, _BINDING)

    with pytest.raises(_REFUSED):
        decrypt_secret_values(replace(encrypted, key_id="env:0000000000000000"), provider, _BINDING)


def test_a_format_2_row_marked_as_format_1_does_not_decrypt() -> None:
    """Downgrade attack: refused by default; the unbound read cannot open a bound secret."""
    provider = _provider()
    encrypted = encrypt_secret_values({"k": "v"}, provider, _BINDING)
    downgraded = replace(encrypted, format_version=1)

    with pytest.raises(LegacyFormatRefusedError):
        decrypt_secret_values(downgraded, provider, _BINDING)
    with pytest.raises(_REFUSED):
        decrypt_secret_values(downgraded, provider, _BINDING, accept_format_1=True)


def test_a_format_1_row_marked_as_format_2_does_not_decrypt() -> None:
    """The bound read path cannot open an unbound secret either."""
    with pytest.raises(_REFUSED):
        decrypt_secret_values(replace(LEGACY_SECRET, format_version=2), _provider(), _BINDING)


def test_a_format_1_secret_written_by_the_previous_release_decrypts_only_when_accepted() -> None:
    """Literal bytes from `origin/main`'s code decrypt, for any row, only with the setting on.

    Format 1 is unbound: that is the residual the threat model states.
    """
    provider = _provider()
    elsewhere = SecretBinding(uuid.uuid4(), uuid.uuid4(), "elsewhere")

    for binding in (_BINDING, elsewhere):
        assert (
            decrypt_secret_values(LEGACY_SECRET, provider, binding, accept_format_1=True)
            == LEGACY_VALUES
        )
        with pytest.raises(LegacyFormatRefusedError):
            decrypt_secret_values(LEGACY_SECRET, provider, binding)


class _SpyProvider(EnvKeyProvider):
    """A key provider that records whether anything asked it to unwrap."""

    def __init__(self) -> None:
        super().__init__(base64.b64encode(b"0" * 32).decode())
        self.unwrapped = 0

    def unwrap(self, wrapped: bytes, associated_data: bytes | None = None) -> bytes:
        """Count the call, then unwrap.

        Parameters
        ----------
        wrapped
            The wrapped data key.
        associated_data
            The bytes `wrap` was given.

        Returns
        -------
        bytes
            The data key.
        """
        self.unwrapped += 1
        return super().unwrap(wrapped, associated_data)


def test_a_refused_format_1_row_is_refused_before_any_decryption() -> None:
    """With the setting off nothing is unwrapped or decrypted: the refusal comes first."""
    spy = _SpyProvider()

    with pytest.raises(LegacyFormatRefusedError, match="not accepted"):
        decrypt_secret_values(LEGACY_SECRET, spy, _BINDING)

    assert spy.unwrapped == 0


@pytest.mark.parametrize("format_version", [-1, 0, 1, 2, 3, 255, 2**31 - 1])
@pytest.mark.parametrize("key_id", ["env", "", "env:0000000000000000", "x" * 300])
def test_no_row_value_makes_a_format_1_triple_decrypt_with_the_setting_off(
    format_version: int, key_id: str
) -> None:
    """Attacker with database write access, no master key, setting off: nothing decrypts.

    The old-format triple is pasted with every `format_version` and `key_id` tried; the
    bound path rejects it on both layers, and every other version is refused outright.
    """
    row = replace(LEGACY_SECRET, format_version=format_version, key_id=key_id)

    with pytest.raises((SecretFormatError, InvalidTag, UnwrapError)):
        decrypt_secret_values(row, _provider(), _BINDING)


@pytest.mark.parametrize("key_id", ["env", "", "env:0000000000000000", "anything at all"])
def test_a_format_1_rows_key_id_is_ignored_when_it_is_accepted(key_id: str) -> None:
    """Format 1 never bound the key id: any value on the row decrypts when accepted."""
    row = replace(LEGACY_SECRET, key_id=key_id)

    assert decrypt_secret_values(row, _provider(), _BINDING, accept_format_1=True) == LEGACY_VALUES


def test_a_format_1_triple_pasted_onto_a_format_2_row() -> None:
    """The documented residual: refused with the setting off, accepted with it on.

    An attacker with database write access and no master key takes a format-1 triple
    ever written under this master key and writes it, with `format_version=1`, onto a row
    of their choice. The default refuses it; the setting accepts any old-format ciphertext.
    """
    provider = _provider()
    victim = encrypt_secret_values({"k": "v"}, provider, _BINDING)
    pasted = replace(
        victim,
        ciphertext=LEGACY_SECRET.ciphertext,
        nonce=LEGACY_SECRET.nonce,
        wrapped_data_key=LEGACY_SECRET.wrapped_data_key,
        format_version=1,
    )

    with pytest.raises(LegacyFormatRefusedError):
        decrypt_secret_values(pasted, provider, _BINDING)
    assert decrypt_secret_values(pasted, provider, _BINDING, accept_format_1=True) == LEGACY_VALUES


@pytest.mark.parametrize("version", [3, 99, 0, -1])
def test_an_unknown_format_version_is_refused_not_guessed(version: int) -> None:
    """A version this code does not know is never decrypted by trying the known ones."""
    provider = _provider()
    encrypted = encrypt_secret_values({"k": "v"}, provider, _BINDING)

    with pytest.raises(SecretFormatError) as raised:
        decrypt_secret_values(replace(encrypted, format_version=version), provider, _BINDING)
    assert raised.value.format_version == version


def test_the_values_layer_is_bound_on_its_own() -> None:
    """A row whose wrapped key is bound correctly but whose values are not, is refused."""
    provider = _provider()
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    key_id = provider.key_id
    unbound = AESGCM(data_key).encrypt(nonce, b'{"k": "v"}', None)
    wrapped = provider.wrap(data_key, associated_data("wrap", CURRENT_FORMAT, _BINDING, key_id))
    row = EncryptedSecret(unbound, nonce, wrapped, CURRENT_FORMAT, key_id)

    with pytest.raises(InvalidTag):
        decrypt_secret_values(row, provider, _BINDING)


def test_the_wrap_layer_is_bound_on_its_own() -> None:
    """A row whose values are bound correctly but whose wrapped key is not, is refused."""
    provider = _provider()
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    key_id = provider.key_id
    values_aad = associated_data("values", CURRENT_FORMAT, _BINDING, key_id)
    ciphertext = AESGCM(data_key).encrypt(nonce, b'{"k": "v"}', values_aad)
    row = EncryptedSecret(ciphertext, nonce, provider.wrap(data_key), CURRENT_FORMAT, key_id)
    assert decrypt_secret_values(
        replace(
            row,
            wrapped_data_key=provider.wrap(
                data_key, associated_data("wrap", CURRENT_FORMAT, _BINDING, key_id)
            ),
        ),
        provider,
        _BINDING,
    ) == {"k": "v"}

    with pytest.raises(UnwrapError):
        decrypt_secret_values(row, provider, _BINDING)


def test_a_wrapped_key_moved_to_another_row_does_not_unwrap() -> None:
    """A wrapped data key bound to one row cannot be unwrapped for another."""
    provider = _provider()
    other = SecretBinding(uuid.uuid4(), _ENV, "other")
    key_id = provider.key_id
    wrapped = provider.wrap(b"\x01" * 32, associated_data("wrap", CURRENT_FORMAT, _BINDING, key_id))

    with pytest.raises(UnwrapError):
        provider.unwrap(wrapped, associated_data("wrap", CURRENT_FORMAT, other, key_id))


def test_the_associated_data_has_one_exact_encoding() -> None:
    """Five length-prefixed fields: context, id, environment id, name, key id."""
    expected = (
        b"\x00\x00\x00\x17lakana.secret.values.v2"
        + b"\x00\x00\x00\x10"
        + _ID.bytes
        + b"\x00\x00\x00\x10"
        + _ENV.bytes
        + b"\x00\x00\x00\x08s3-creds"
        + b"\x00\x00\x00\x05env:a"
    )
    assert associated_data("values", 2, _BINDING, "env:a") == expected


def test_the_associated_data_of_two_different_rows_is_never_equal() -> None:
    """Adversarial names, key ids, purposes and versions never collide."""
    names = ["", "a", "ab", "a\x00b", "\x00\x00\x00\x01a", "s3-creds", "s3-creds\x00", "é", "Ǟ"]
    key_ids = ["", "c", "bc", "env", "env:0123456789abcdef", "\x00\x00\x00\x03abc", "a" * 64]
    ids = [uuid.UUID(int=0), uuid.UUID(int=1), uuid.UUID(int=2**64)]
    seen: dict[bytes, tuple] = {}
    for purpose, version, secret_id, env_id, name, key_id in itertools.product(
        ("values", "wrap"), (1, 2, 3), ids, ids, names, key_ids
    ):
        inputs = (purpose, version, secret_id, env_id, name, key_id)
        data = associated_data(purpose, version, SecretBinding(secret_id, env_id, name), key_id)
        assert seen.setdefault(data, inputs) == inputs, (inputs, seen[data])


def test_decrypt_secret_row_reads_a_format_2_row() -> None:
    """The row-level read takes the binding from the row itself."""
    provider = _provider()
    row = _row(_BINDING, encrypt_secret_values({"k": "v"}, provider, _BINDING))

    assert decrypt_secret_row(row, provider, environment_name="dev") == {"k": "v"}


def test_decrypt_secret_row_reads_a_format_1_row_only_when_accepted() -> None:
    """The row-level read opens the previous release's rows only with the setting on."""
    row = _row(_BINDING, LEGACY_SECRET)

    assert (
        decrypt_secret_row(row, _provider(), environment_name="dev", accept_format_1=True)
        == LEGACY_VALUES
    )


def test_a_refused_format_1_row_says_why_and_what_to_do(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The refusal names the secret, the two ways forward and the setting; one WARNING."""
    row = _row(_BINDING, LEGACY_SECRET)
    provider = _SpyProvider()

    with (
        caplog.at_level(logging.WARNING, logger="lakana_core.secrets.crypto"),
        pytest.raises(LakanaError) as raised,
    ):
        decrypt_secret_row(row, provider, environment_name="dev")

    assert str(raised.value) == (
        "Secret 's3-creds' in environment 'dev' is stored in the old format (1), which Lakana "
        "does not read by default. Set it again with `lakana secret rotate s3-creds --env dev "
        "--values-stdin`; that needs no read of the old value. To keep old secrets working "
        "until you have, start the server and the workers with "
        "LAKANA_SECRETS__ACCEPT_FORMAT_1=true, and remove the setting afterwards."
    )
    assert not isinstance(raised.value, SecretFormatError)
    assert provider.unwrapped == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(row.id) in warnings[0].getMessage()
    assert "LAKANA_SECRETS__ACCEPT_FORMAT_1" in warnings[0].getMessage()


def test_a_refused_format_1_row_quotes_a_hostile_name_for_the_shell() -> None:
    """The command in the message is meant to be copied: the name is shell-quoted."""
    row = _row(replace(_BINDING, name="a b; rm -rf x"), LEGACY_SECRET)

    with pytest.raises(LakanaError) as raised:
        decrypt_secret_row(row, _provider(), environment_name="dev")

    assert "`lakana secret rotate 'a b; rm -rf x' --env dev --values-stdin`" in str(raised.value)


def test_the_accept_warning_says_what_it_weakens_and_how_to_finish() -> None:
    """The start-up text names the setting, the weakened binding and the way out."""
    assert "LAKANA_SECRETS__ACCEPT_FORMAT_1" in ACCEPT_FORMAT_1_WARNING
    assert "any old-format ciphertext" in ACCEPT_FORMAT_1_WARNING
    assert "lakana secret list" in ACCEPT_FORMAT_1_WARNING
    assert "lakana secret rotate" in ACCEPT_FORMAT_1_WARNING
    assert "remove the setting" in ACCEPT_FORMAT_1_WARNING.lower()


def test_a_refused_row_gets_an_opaque_error_and_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The error names the secret and the next step; the log has the row's coordinates."""
    provider = _provider()
    moved = _row(
        SecretBinding(uuid.uuid4(), _ENV, "s3-creds"),
        encrypt_secret_values({"k": "v"}, provider, _BINDING),
    )

    with (
        caplog.at_level(logging.WARNING, logger="lakana_core.secrets.crypto"),
        pytest.raises(LakanaError) as raised,
    ):
        decrypt_secret_row(moved, provider, environment_name="dev")

    assert str(raised.value) == _UNDECRYPTABLE
    for forbidden in ("tag", "mismatch", "AAD", "associated", "environment id", "format"):
        assert forbidden not in str(raised.value)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert str(moved.id) in text and "format_version=2" in text
    assert f"key_id={provider.key_id}" in text
    assert f"configured_key_id={provider.key_id}" in text


def test_a_wrong_master_key_gets_the_same_message_and_a_log_that_tells_it_apart(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same answer for the operator, whatever failed; the log has both key ids."""
    row = _row(_BINDING, encrypt_secret_values({"k": "v"}, _provider(), _BINDING))
    other = EnvKeyProvider(base64.b64encode(b"1" * 32).decode())

    with (
        caplog.at_level(logging.WARNING, logger="lakana_core.secrets.crypto"),
        pytest.raises(LakanaError) as raised,
    ):
        decrypt_secret_row(row, other, environment_name="dev")

    assert str(raised.value) == _UNDECRYPTABLE
    text = caplog.records[0].getMessage()
    assert f"key_id={_provider().key_id}" in text
    assert f"configured_key_id={other.key_id}" in text
    assert _provider().key_id != other.key_id


@pytest.mark.parametrize("field", ["nonce", "wrapped_data_key", "ciphertext"])
@pytest.mark.parametrize("value", [b"", b"\x00"], ids=["empty", "one byte"])
@pytest.mark.parametrize("format_version", [1, 2])
def test_a_malformed_stored_field_is_as_opaque_as_a_moved_row(
    caplog: pytest.LogCaptureFixture, field: str, value: bytes, format_version: int
) -> None:
    """A field of the wrong shape raises no raw `ValueError`: one message, one WARNING."""
    provider = _provider()
    source = encrypt_secret_values({"k": "v"}, provider, _BINDING)
    if format_version == 1:
        source = LEGACY_SECRET
    row = _row(_BINDING, replace(source, **{field: value}))

    with (
        caplog.at_level(logging.WARNING, logger="lakana_core.secrets.crypto"),
        pytest.raises(LakanaError) as raised,
    ):
        decrypt_secret_row(row, provider, environment_name="dev", accept_format_1=True)

    assert str(raised.value) == _UNDECRYPTABLE
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_an_unknown_format_logs_even_without_a_usable_master_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A format-0 row with no key configured still gets its one WARNING."""
    row = _row(_BINDING, replace(LEGACY_SECRET, format_version=0))

    with (
        caplog.at_level(logging.WARNING, logger="lakana_core.secrets.crypto"),
        pytest.raises(LakanaError, match="could not be decrypted"),
    ):
        decrypt_secret_row(row, EnvKeyProvider(None), environment_name="dev")

    assert "configured_key_id=unavailable" in caplog.text


def test_a_missing_master_key_keeps_its_own_message() -> None:
    """Not having a key is a configuration error, not a damaged row."""
    row = _row(_BINDING, encrypt_secret_values({"k": "v"}, _provider(), _BINDING))

    with pytest.raises(LakanaError, match="No secrets master key is configured"):
        decrypt_secret_row(row, EnvKeyProvider(None), environment_name="dev")


def test_a_row_of_a_newer_format_says_to_upgrade(caplog: pytest.LogCaptureFixture) -> None:
    """An unknown, higher format version is refused with an upgrade instruction."""
    provider = _provider()
    row = _row(_BINDING, encrypt_secret_values({"k": "v"}, provider, _BINDING))
    row.format_version = 3

    with (
        caplog.at_level(logging.WARNING, logger="lakana_core.secrets.crypto"),
        pytest.raises(LakanaError) as raised,
    ):
        decrypt_secret_row(row, provider, environment_name="dev")

    message = str(raised.value)
    assert "'s3-creds'" in message and "newer Lakana" in message and "Upgrade" in message
    assert "format_version=3" in caplog.text


def test_a_row_of_a_format_below_one_is_refused_as_undecryptable() -> None:
    """Format 0 was never written by any release: it is damage, not a newer Lakana."""
    provider = _provider()
    row = _row(_BINDING, encrypt_secret_values({"k": "v"}, provider, _BINDING))
    row.format_version = 0

    with pytest.raises(LakanaError, match="could not be decrypted"):
        decrypt_secret_row(row, provider, environment_name="dev")
