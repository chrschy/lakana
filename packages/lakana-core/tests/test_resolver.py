"""Tests for storage resolution."""

import base64
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import NamespacePath
from lakana_core.models import Environment, Principal, PrincipalType, Secret, SecretUseKind
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.secrets.uses import SecretUse, SecretUseRefused
from lakana_core.storage.resolver import (
    _FORBIDDEN_TABLE_PROPERTIES,
    _PATH_TABLE_PROPERTIES,
    StorageResolver,
    _parse_scheme,
    is_id_suffixed_location,
    is_owned_by_location,
    is_owned_by_location_given_nested,
    is_within_location,
    nested_locations_within,
)


@pytest.fixture
def environment(session: Session, tmp_path: Path) -> Environment:
    """Persist an environment rooted at a temporary directory.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Pytest-provided temporary directory.

    Returns
    -------
    Environment
        The saved environment.
    """
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root=f"file://{tmp_path}",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(env)
    session.commit()
    return env


def test_resolves_a_known_environment(session: Session, environment: Environment) -> None:
    """Resolution yields usable storage for an existing environment."""
    resolved = StorageResolver(session).for_environment("dev")
    resolved.storage.write("probe.txt", b"ok")
    assert resolved.storage.read("probe.txt") == b"ok"


def test_unknown_environment_raises_not_found(session: Session) -> None:
    """Resolving an environment that does not exist is an error."""
    with pytest.raises(NotFoundError):
        StorageResolver(session).for_environment("nope")


@pytest.mark.parametrize("scheme", ["s3", "abfs"])
def test_parse_scheme_recognises_object_storage_schemes(scheme: str) -> None:
    """Object-storage schemes are recognised by the scheme parser.

    This exercises the parser directly rather than the full resolution
    path, because their fsspec filesystem implementations (s3fs, adlfs)
    are not lakana-core dependencies.
    """
    assert _parse_scheme(f"{scheme}://bucket/prefix") == scheme


def test_unrecognised_scheme_raises_validation_error(session: Session) -> None:
    """A storage root with an unsupported scheme cannot be resolved."""
    env = Environment(
        id=uuid.uuid4(),
        name="broken",
        storage_root="ftp://example.com/data",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(env)
    session.commit()
    with pytest.raises(ValidationError):
        StorageResolver(session).for_environment("broken")


def test_table_location_nests_namespace_and_table(
    session: Session, environment: Environment
) -> None:
    """A table's location is under managed/<namespace path>/<table>-<id hex>."""
    resolved = StorageResolver(session).for_environment("dev")
    table_id = uuid.uuid4()
    location = resolved.table_location(
        NamespacePath.from_parts(["gold", "sales"]), "monthly", table_id
    )
    assert location.startswith(environment.storage_root)
    assert location.endswith(f"/managed/gold/sales/monthly-{table_id.hex}")


def test_table_location_rejects_a_traversal_table_name(
    session: Session, environment: Environment
) -> None:
    """A table name that escapes the namespace must not reach `file_io`.

    Reproduces the bug this guards against: `NamespacePath` alone cannot
    close this, because the table name is not a namespace component and so
    is never passed through `NamespacePath.from_parts`.
    """
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError):
        resolved.table_location(NamespacePath.from_parts(["gold"]), "../../pwn", uuid.uuid4())


@pytest.mark.parametrize("table", [".", "..", "a/b", "", "  "])
def test_table_location_rejects_invalid_table_names(
    session: Session, environment: Environment, table: str
) -> None:
    """`table_location` is the choke point for the table name, not just the namespace."""
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError):
        resolved.table_location(NamespacePath.from_parts(["gold"]), table, uuid.uuid4())


def test_table_location_does_not_collide_across_slash_and_split_components(
    session: Session, environment: Environment
) -> None:
    """A namespace component containing '/' is rejected, not merged with a split one.

    Before this guard, `table_location` built `["a/b"]` and `["a", "b"]`
    into the identical filesystem path by joining parts with '/', which
    would let two distinct catalog namespaces collide on one storage
    directory.
    """
    with pytest.raises(ValidationError):
        NamespacePath.from_parts(["a/b"])
    resolved = StorageResolver(session).for_environment("dev")
    table_id = uuid.uuid4()
    split_location = resolved.table_location(NamespacePath.from_parts(["a", "b"]), "t", table_id)
    assert split_location.endswith(f"/managed/a/b/t-{table_id.hex}")


def test_confine_to_root_accepts_the_root_itself_and_a_nested_path(
    session: Session, environment: Environment
) -> None:
    """A location equal to, or nested inside, the root is accepted unchanged."""
    resolved = StorageResolver(session).for_environment("dev")
    assert resolved.confine_to_root(environment.storage_root) == environment.storage_root
    nested = f"{environment.storage_root}/custom/sales"
    assert resolved.confine_to_root(nested) == nested


def test_confine_to_root_rejects_an_unrelated_path(
    session: Session, environment: Environment
) -> None:
    """A location outside the root entirely is refused."""
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError):
        resolved.confine_to_root("file:///tmp/OUTSIDE/pwn")


def test_confine_to_root_rejects_a_traversal(session: Session, environment: Environment) -> None:
    """A ``..`` segment that normalises outside the root is refused, not just a bare path."""
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError):
        resolved.confine_to_root(f"{environment.storage_root}/../TRAVERSED")


def test_confine_to_root_rejects_a_sibling_that_merely_shares_a_prefix(
    session: Session, environment: Environment
) -> None:
    """`.../<root>evil` must not pass a substring check as being inside `.../<root>`."""
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError):
        resolved.confine_to_root(f"{environment.storage_root}evil/pwn")


def test_confine_to_root_rejects_a_mismatched_scheme(
    session: Session, environment: Environment
) -> None:
    """A location under a different scheme than the root's can never be inside it."""
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError):
        resolved.confine_to_root("s3://bucket/pwn")


def test_confine_to_root_rejects_a_nul_byte(session: Session, environment: Environment) -> None:
    """A NUL byte is rejected here too, not only by `FsspecStorage._full`.

    Without this check, a NUL byte in a caller-supplied location reaches
    `file_io` unrejected and surfaces as a raw third-party exception
    (pyarrow's `ArrowInvalid`) instead of `lakana-core`'s own
    `ValidationError` — the one path where that leaked.
    """
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError):
        resolved.confine_to_root(f"{environment.storage_root}/ba\x00d")


@pytest.mark.parametrize(
    "key", ["write.metadata.path", "write.data.path", "write.object-storage.path"]
)
def test_validate_table_properties_refuses_path_valued_properties(
    session: Session, environment: Environment, key: str
) -> None:
    """Every path-valued table property is refused outright, not merely confined.

    Lakana assigns every managed
    table's location itself; a caller-supplied value for one of these
    properties could otherwise redirect a table's metadata or data to an
    arbitrary in-root location, including one colliding with another
    table's own directory — confining it to the storage root was not
    enough to prevent that collision, so all three are refused whatever
    their value.
    """
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError, match="Lakana assigns table locations") as excinfo:
        resolved.validate_table_properties({key: "file:///tmp/OUTSIDE/pwn"})
    assert key in str(excinfo.value)


@pytest.mark.parametrize(
    "key", ["write.metadata.path", "write.data.path", "write.object-storage.path"]
)
def test_validate_table_properties_refuses_a_path_valued_property_even_inside_the_root(
    session: Session, environment: Environment, key: str
) -> None:
    """The refusal does not depend on the value: an in-root value is refused too."""
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError, match="Lakana assigns table locations"):
        resolved.validate_table_properties({key: f"{environment.storage_root}/elsewhere"})


def test_validate_table_properties_rejects_the_location_provider_class(
    session: Session, environment: Environment
) -> None:
    """`write.py-location-provider.impl` is refused whatever its value.

    pyiceberg's location-provider loader `importlib.import_module`s the
    module named by this property and instantiates the class. Confining a
    path cannot defend against that, because the import has already run by
    the time any path exists.
    """
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError) as excinfo:
        resolved.validate_table_properties({"write.py-location-provider.impl": "os.system"})
    assert "write.py-location-provider.impl" in str(excinfo.value)


def test_validate_table_properties_ignores_unrelated_properties(
    session: Session, environment: Environment
) -> None:
    """Properties the guard does not name pass through untouched."""
    resolved = StorageResolver(session).for_environment("dev")
    resolved.validate_table_properties({"owner": "analytics", "format-version": "2"})


def test_the_guarded_property_keys_are_the_ones_pyiceberg_reads() -> None:
    """The guard is pinned to pyiceberg's own constants and their values.

    Referencing `TableProperties` means a renamed *constant* fails at
    import. This asserts the other half: that the wire keys behind those
    constants are still the ones the guard was written for, so a renamed
    *value* fails here rather than silently ceasing to match.
    """
    assert {"write.py-location-provider.impl"} == _FORBIDDEN_TABLE_PROPERTIES
    assert {
        "write.metadata.path",
        "write.data.path",
        "write.object-storage.path",
    } == _PATH_TABLE_PROPERTIES


def test_for_environment_ignores_storage_secret_ref_when_it_is_none(session: Session) -> None:
    """The overwhelmingly common case: no secret_ref means behavior is unchanged."""
    from lakana_core.models import Environment
    from lakana_core.storage.resolver import StorageResolver

    env = Environment(
        id=uuid.uuid4(), name="dev", storage_root="file:///tmp/dev", storage_options={"a": "b"}
    )
    session.add(env)
    session.commit()

    resolved = StorageResolver(session).for_environment("dev")

    assert resolved.config.options == {"a": "b"}


def test_for_environment_merges_a_decrypted_secret_into_options(session: Session) -> None:
    """A secret-backed environment's decrypted values reach the built StorageConfig."""
    import base64

    from lakana_core.models import Environment, Principal, PrincipalType, Secret
    from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
    from lakana_core.secrets.keys import EnvKeyProvider
    from lakana_core.storage.resolver import StorageResolver

    admin = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    session.add(admin)
    session.commit()
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/dev",
        storage_options={"region": "us-east-1"},
        storage_secret_ref="s3-creds",
    )
    session.add(env)
    session.commit()
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    secret_id = uuid.uuid4()
    encrypted = encrypt_secret_values(
        {"access_key_id": "AKIA123", "secret_access_key": "shh"},
        provider,
        SecretBinding(secret_id, env.id, "s3-creds"),
    )
    session.add(
        Secret(
            id=secret_id,
            environment_id=env.id,
            name="s3-creds",
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            wrapped_data_key=encrypted.wrapped_data_key,
            format_version=encrypted.format_version,
            key_id=encrypted.key_id,
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()

    resolved = StorageResolver(session, provider).for_environment("dev")

    assert resolved.config.options == {
        "region": "us-east-1",
        "access_key_id": "AKIA123",
        "secret_access_key": "shh",
    }


def test_resolved_storage_repr_never_prints_a_merged_secret_value(session: Session) -> None:
    """`ResolvedStorage`'s repr never leaks a secret merged into its config."""
    import base64

    from lakana_core.models import Environment, Principal, PrincipalType, Secret
    from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
    from lakana_core.secrets.keys import EnvKeyProvider
    from lakana_core.storage.resolver import StorageResolver

    admin = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    session.add(admin)
    session.commit()
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/dev",
        storage_options={"region": "us-east-1"},
        storage_secret_ref="s3-creds",
    )
    session.add(env)
    session.commit()
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    secret_id = uuid.uuid4()
    encrypted = encrypt_secret_values(
        {"access_key_id": "AKIA123", "secret_access_key": "shh"},
        provider,
        SecretBinding(secret_id, env.id, "s3-creds"),
    )
    session.add(
        Secret(
            id=secret_id,
            environment_id=env.id,
            name="s3-creds",
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            wrapped_data_key=encrypted.wrapped_data_key,
            format_version=encrypted.format_version,
            key_id=encrypted.key_id,
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()

    resolved = StorageResolver(session, provider).for_environment("dev")

    text = repr(resolved)
    assert "AKIA123" not in text
    assert "shh" not in text
    assert "file:///tmp/dev" in text
    assert "access_key_id" in text
    # Behaviour is unchanged: the real secret is still reachable.
    assert resolved.config.options["secret_access_key"] == "shh"


def test_secret_fields_win_over_a_stale_plaintext_option_on_key_collision(session: Session) -> None:
    """A secret must never be silently shadowed by a stale plaintext option."""
    import base64

    from lakana_core.models import Environment, Principal, PrincipalType, Secret
    from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
    from lakana_core.secrets.keys import EnvKeyProvider
    from lakana_core.storage.resolver import StorageResolver

    admin = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    session.add(admin)
    session.commit()
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/dev",
        storage_options={"access_key_id": "STALE_PLAINTEXT_VALUE"},
        storage_secret_ref="s3-creds",
    )
    session.add(env)
    session.commit()
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    secret_id = uuid.uuid4()
    encrypted = encrypt_secret_values(
        {"access_key_id": "REAL_VALUE"}, provider, SecretBinding(secret_id, env.id, "s3-creds")
    )
    session.add(
        Secret(
            id=secret_id,
            environment_id=env.id,
            name="s3-creds",
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            wrapped_data_key=encrypted.wrapped_data_key,
            format_version=encrypted.format_version,
            key_id=encrypted.key_id,
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()

    resolved = StorageResolver(session, provider).for_environment("dev")

    assert resolved.config.options["access_key_id"] == "REAL_VALUE"


def test_for_environment_raises_a_clear_error_when_a_secret_is_needed_but_no_key_provider_given(
    session: Session,
) -> None:
    """A secret-backed environment with no KeyProvider fails clearly, not with an AttributeError."""
    from lakana_core.errors import ValidationError
    from lakana_core.models import Environment
    from lakana_core.storage.resolver import StorageResolver

    env = Environment(
        id=uuid.uuid4(), name="dev", storage_root="file:///tmp/dev", storage_secret_ref="s3-creds"
    )
    session.add(env)
    session.commit()

    with pytest.raises(ValidationError, match="key_provider"):
        StorageResolver(session).for_environment("dev")


def test_for_environment_raises_not_found_for_a_dangling_secret_ref(session: Session) -> None:
    """A storage_secret_ref naming a secret that doesn't exist fails closed, not silently."""
    import base64

    from lakana_core.errors import NotFoundError
    from lakana_core.models import Environment
    from lakana_core.secrets.keys import EnvKeyProvider
    from lakana_core.storage.resolver import StorageResolver

    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/dev",
        storage_secret_ref="does-not-exist",
    )
    session.add(env)
    session.commit()
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())

    with pytest.raises(NotFoundError, match="does-not-exist"):
        StorageResolver(session, provider).for_environment("dev")


def test_for_environment_raises_not_found_for_a_revoked_secret(session: Session) -> None:
    """A revoked secret must not be resolvable, even though the row still exists."""
    import base64

    from lakana_core.errors import NotFoundError
    from lakana_core.models import Environment, Principal, PrincipalType, Secret
    from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
    from lakana_core.secrets.keys import EnvKeyProvider
    from lakana_core.storage.resolver import StorageResolver

    admin = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    session.add(admin)
    session.commit()
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/dev",
        storage_secret_ref="s3-creds",
    )
    session.add(env)
    session.commit()
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    secret_id = uuid.uuid4()
    encrypted = encrypt_secret_values(
        {"access_key_id": "AKIA123"}, provider, SecretBinding(secret_id, env.id, "s3-creds")
    )
    session.add(
        Secret(
            id=secret_id,
            environment_id=env.id,
            name="s3-creds",
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            wrapped_data_key=encrypted.wrapped_data_key,
            format_version=encrypted.format_version,
            key_id=encrypted.key_id,
            created_by=admin.id,
            created_at=datetime.now(UTC),
            revoked_by=admin.id,
            revoked_at=datetime.now(UTC),
        )
    )
    session.commit()

    with pytest.raises(NotFoundError, match="s3-creds"):
        StorageResolver(session, provider).for_environment("dev")


def test_for_environment_raises_lakana_error_for_tampered_ciphertext(session: Session) -> None:
    """Corrupted ciphertext surfaces as LakanaError, not a raw cryptography.InvalidTag.

    `secrets.crypto.decrypt_secret_row` translates the integrity failure into the
    domain's own error, naming the secret and saying what to do.
    """
    import base64

    from lakana_core.errors import LakanaError
    from lakana_core.models import Environment, Principal, PrincipalType, Secret
    from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
    from lakana_core.secrets.keys import EnvKeyProvider
    from lakana_core.storage.resolver import StorageResolver

    admin = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    session.add(admin)
    session.commit()
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/dev",
        storage_secret_ref="s3-creds",
    )
    session.add(env)
    session.commit()
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    secret_id = uuid.uuid4()
    encrypted = encrypt_secret_values(
        {"access_key_id": "AKIA123"}, provider, SecretBinding(secret_id, env.id, "s3-creds")
    )
    tampered_ciphertext = bytes([encrypted.ciphertext[0] ^ 0xFF]) + encrypted.ciphertext[1:]
    session.add(
        Secret(
            id=secret_id,
            environment_id=env.id,
            name="s3-creds",
            ciphertext=tampered_ciphertext,
            nonce=encrypted.nonce,
            wrapped_data_key=encrypted.wrapped_data_key,
            format_version=encrypted.format_version,
            key_id=encrypted.key_id,
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()

    with pytest.raises(LakanaError, match="s3-creds"):
        StorageResolver(session, provider).for_environment("dev")


def test_for_environment_refuses_a_format_1_secret_unless_the_provider_accepts_it(
    session: Session,
) -> None:
    """The resolver hands the provider's setting to the decrypt: off refuses, on reads."""
    import base64
    import json

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from lakana_core.errors import LakanaError
    from lakana_core.models import Environment, Principal, PrincipalType, Secret
    from lakana_core.secrets.keys import EnvKeyProvider
    from lakana_core.storage.resolver import StorageResolver

    admin = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/dev",
        storage_secret_ref="s3-creds",
    )
    session.add_all([admin, env])
    session.commit()
    master = base64.b64encode(b"0" * 32).decode()
    provider = EnvKeyProvider(master)
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = b"\x07" * 12
    session.add(
        Secret(
            id=uuid.uuid4(),
            environment_id=env.id,
            name="s3-creds",
            ciphertext=AESGCM(data_key).encrypt(nonce, json.dumps({"k": "v"}).encode(), None),
            nonce=nonce,
            wrapped_data_key=provider.wrap(data_key),
            format_version=1,
            key_id="env",
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()

    with pytest.raises(LakanaError, match="stored in the old format"):
        StorageResolver(session, provider).for_environment("dev")
    accepting = EnvKeyProvider(master, accept_format_1=True)
    resolved = StorageResolver(session, accepting).for_environment("dev")
    assert resolved.config.options["k"] == "v"


def test_for_environment_does_not_resolve_a_same_named_secret_from_another_environment(
    session: Session,
) -> None:
    """The secret lookup is scoped by environment_id, not just by name."""
    import base64

    from lakana_core.errors import NotFoundError
    from lakana_core.models import Environment, Principal, PrincipalType, Secret
    from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
    from lakana_core.secrets.keys import EnvKeyProvider
    from lakana_core.storage.resolver import StorageResolver

    admin = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    session.add(admin)
    session.commit()
    other_env = Environment(
        id=uuid.uuid4(),
        name="other",
        storage_root="file:///tmp/other",
        storage_options={},
    )
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/dev",
        storage_secret_ref="s3-creds",
    )
    session.add_all([other_env, env])
    session.commit()
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    secret_id = uuid.uuid4()
    encrypted = encrypt_secret_values(
        {"access_key_id": "AKIA123"}, provider, SecretBinding(secret_id, other_env.id, "s3-creds")
    )
    session.add(
        Secret(
            id=secret_id,
            environment_id=other_env.id,
            name="s3-creds",
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            wrapped_data_key=encrypted.wrapped_data_key,
            format_version=encrypted.format_version,
            key_id=encrypted.key_id,
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()

    with pytest.raises(NotFoundError, match="s3-creds"):
        StorageResolver(session, provider).for_environment("dev")


def test_is_within_location_accepts_a_canonical_nested_path() -> None:
    """A straightforward file under a table's location is within it."""
    assert is_within_location(
        "file:///root/managed/gold/orders-abc",
        "file:///root/managed/gold/orders-abc/data/x.parquet",
    )


def test_is_within_location_rejects_a_sibling_with_shared_string_prefix() -> None:
    """A sibling directory that is merely a string prefix match is not within it."""
    assert not is_within_location(
        "file:///root/managed/gold/orders-abc",
        "file:///root/managed/gold/orders-abc2/data/x.parquet",
    )


def test_is_within_location_rejects_a_dot_dot_traversal() -> None:
    """A `..`-laden URI that textually starts with `location` is refused, not accepted.

    A naive `str.startswith` check would
    accept this: the string
    ``.../attacker-<hex>/../customers-<hex>/data/x.parquet`` literally begins
    with ``.../attacker-<hex>/``. But a storage backend normalises paths
    before opening them (`FsspecStorage._full` calls `posixpath.normpath`),
    so this URI actually resolves to a file under a completely different
    table's directory. Refusing any non-canonical URI outright closes this
    regardless of what it would normalise to.
    """
    assert not is_within_location(
        "file:///root/managed/gold/attacker-abc",
        "file:///root/managed/gold/attacker-abc/../customers-def/data/x.parquet",
    )


def test_is_within_location_rejects_a_doubled_slash() -> None:
    """A doubled slash is non-canonical and refused, the same as a `..` segment."""
    assert not is_within_location(
        "file:///root/managed/gold/orders-abc",
        "file:///root/managed/gold//orders-abc/data/x.parquet",
    )


def test_is_within_location_rejects_a_uri_with_no_scheme() -> None:
    """A URI missing a `scheme://` prefix fails closed rather than raising."""
    assert not is_within_location(
        "file:///root/managed/gold/orders-abc", "/root/managed/gold/orders-abc/x"
    )


def test_is_within_location_rejects_a_mismatched_scheme_spelling() -> None:
    """A different spelling of the same backend's scheme (`s3a://` vs `s3://`) fails closed.

    This is not detected or reported
    specially: the literal string prefix simply never matches, so the file
    is refused the same way an unrelated path is.
    """
    assert not is_within_location(
        "s3://bucket/managed/gold/orders-abc", "s3a://bucket/managed/gold/orders-abc/data/x.parquet"
    )


def test_is_owned_by_location_true_for_its_own_files_with_no_other_tables() -> None:
    """A table's own file is owned by it when there is nothing else to contest it."""
    assert is_owned_by_location(
        "file:///root/managed/gold/orders-abc",
        "file:///root/managed/gold/orders-abc/data/x.parquet",
        [],
    )


def test_is_owned_by_location_true_when_other_locations_are_unrelated() -> None:
    """An unrelated sibling table's location never affects ownership of this table's files."""
    assert is_owned_by_location(
        "file:///root/managed/gold/orders-abc",
        "file:///root/managed/gold/orders-abc/data/x.parquet",
        ["file:///root/managed/gold/customers-def"],
    )


def test_is_owned_by_location_false_for_a_legacy_root_location_table() -> None:
    """A legacy table whose location is the environment root cannot claim another's files.

    Once, `confine_to_root` allowed a caller-supplied
    `location` to be the environment root itself. Such a table's location
    textually contains every other table's own (id-suffixed) directory, so
    without the "most specific owner" rule it could claim any other table's
    data file as its own.
    """
    root_table_location = "file:///root"
    other_table_location = "file:///root/managed/gold/customers-def"
    victim_file = f"{other_table_location}/data/x.parquet"
    assert not is_owned_by_location(root_table_location, victim_file, [other_table_location])


def test_is_owned_by_location_false_for_a_legacy_parent_directory_table() -> None:
    """A legacy table at an intermediate namespace directory cannot claim a child table's files.

    A older table named `orders` at `managed/gold/orders` (no id
    suffix) sits at the same path a child namespace `gold.orders` would use
    for its own tables (`managed/gold/orders/<t>-<hex>`). The nested table
    `t`'s own file must be excluded from the legacy `orders` table.
    """
    legacy_table_location = "file:///root/managed/gold/orders"
    child_namespace_table_location = "file:///root/managed/gold/orders/t-abc"
    child_table_file = f"{child_namespace_table_location}/data/x.parquet"
    assert not is_owned_by_location(
        legacy_table_location, child_table_file, [child_namespace_table_location]
    )


def test_is_owned_by_location_true_beside_a_coincidentally_named_nested_table() -> None:
    """A table's own files are unaffected by an unrelated, normally-created nested table.

    `t` has location `.../T-<hex>` (an ordinary, id-suffixed
    table). A namespace happens to be named literally `T-<hex>` too, and
    holds its own table `u` at `.../T-<hex>/u-<hex2>` — nested inside `t`'s
    location purely by naming coincidence, not because `t` is a legacy
    table or `u` is really `t`'s child. `t`'s own file, which does not fall
    under `u`'s location, must still be attributed to `t`.
    """
    t_location = "file:///root/managed/gold/T-abc"
    u_location = "file:///root/managed/gold/T-abc/U-def"
    t_own_file = f"{t_location}/data/x.parquet"
    assert is_owned_by_location(t_location, t_own_file, [u_location])


def test_is_owned_by_location_false_for_the_nested_tables_own_file() -> None:
    """The reverse of the naming-coincidence scenario: `u`'s own file is not `t`'s.

    Continues `test_is_owned_by_location_true_beside_a_coincidentally_named_nested_table`:
    `u`'s own file, which does lie under its own nested location, must be
    excluded from `t` even though `t`'s location textually contains it.
    """
    t_location = "file:///root/managed/gold/T-abc"
    u_location = "file:///root/managed/gold/T-abc/U-def"
    u_own_file = f"{u_location}/data/y.parquet"
    assert not is_owned_by_location(t_location, u_own_file, [u_location])


@pytest.mark.parametrize("metachar", ["*", "?", "[", "]"])
def test_is_within_location_rejects_a_glob_metacharacter(metachar: str) -> None:
    """A path containing a glob metacharacter is refused, even with a broad `location`.

    Reproduces the
    exploit: a broad (legacy) table's location naively "contains" a data
    file path with wildcards standing in for the namespace and table
    directories -- ``.../managed/*/*/data/*.parquet`` starts with the
    environment root as a bare string, is already `posixpath.normpath`-
    stable (no `.`/`..`/doubled slash), and so passed the round-1 canonical
    check. DuckDB's `read_parquet` expands a glob in a local path itself, so
    a single "file path" like this would match every table's data across
    the whole `managed/` zone once opened.
    """
    root_location = "file:///root"
    glob_path = f"file:///root/managed/gold/data/a{metachar}.parquet"
    assert not is_within_location(root_location, glob_path)


def test_is_within_location_rejects_the_reproduced_glob_exploit() -> None:
    """The exact reproduction from the finding: a broad location, a fully wildcarded suffix."""
    assert not is_within_location("file:///root", "file:///root/managed/*/*/data/*.parquet")


def test_is_within_location_accepts_a_literal_path_with_no_glob_metacharacters() -> None:
    """A normal, glob-free path is unaffected by the new check."""
    assert is_within_location(
        "file:///root/managed/gold/orders-abc",
        "file:///root/managed/gold/orders-abc/data/x.parquet",
    )


def test_is_id_suffixed_location_true_for_a_properly_suffixed_location() -> None:
    """A location ending in `-<table id hex>` is recognised as suffixed."""
    table_id = uuid.uuid4()
    assert is_id_suffixed_location(f"file:///root/managed/gold/orders-{table_id.hex}", table_id)


def test_is_id_suffixed_location_false_for_a_legacy_leaf_directory() -> None:
    """A plain leaf directory not ending in the table's own id hex fails.

    A location without the id fails closed.
    """
    table_id = uuid.uuid4()
    assert not is_id_suffixed_location("file:///root/managed/gold/orders", table_id)


def test_is_id_suffixed_location_false_for_the_environment_root() -> None:
    """The broadest legacy shape -- the environment root itself -- fails."""
    table_id = uuid.uuid4()
    assert not is_id_suffixed_location("file:///root", table_id)


def test_is_id_suffixed_location_false_for_an_intermediate_namespace_directory() -> None:
    """An intermediate namespace directory, another pre-Task-5 shape, fails."""
    table_id = uuid.uuid4()
    assert not is_id_suffixed_location("file:///root/managed/gold", table_id)


def test_is_id_suffixed_location_false_for_another_tables_hex_suffix() -> None:
    """A location suffixed with a *different* table's id is not this table's own."""
    table_id = uuid.uuid4()
    other_id = uuid.uuid4()
    assert not is_id_suffixed_location(f"file:///root/managed/gold/orders-{other_id.hex}", table_id)


def test_is_id_suffixed_location_false_for_a_non_canonical_location() -> None:
    """A `..`-laden location is refused the same way a `..`-laden `uri` is."""
    table_id = uuid.uuid4()
    assert not is_id_suffixed_location(
        f"file:///root/managed/gold/../gold/orders-{table_id.hex}", table_id
    )


def test_is_id_suffixed_location_false_for_a_location_with_a_glob_metacharacter() -> None:
    """A location containing a glob metacharacter is refused, not just a `uri`."""
    table_id = uuid.uuid4()
    assert not is_id_suffixed_location(f"file:///root/managed/gold/*-{table_id.hex}", table_id)


def test_nested_locations_within_filters_to_only_the_nested_ones() -> None:
    """`nested_locations_within` keeps only locations `is_within_location` places inside."""
    location = "file:///root/managed/gold/T-abc"
    nested = "file:///root/managed/gold/T-abc/U-def"
    unrelated = "file:///root/managed/gold/customers-ghi"
    broader_parent = "file:///root/managed/gold"
    assert nested_locations_within(location, [nested, unrelated, broader_parent, location]) == [
        nested
    ]


def test_is_owned_by_location_given_nested_matches_is_owned_by_location() -> None:
    """Precomputing the nested set and using the fast check gives the same answer as the slow one.

    The nested set is precomputed once per
    table. `lakana_worker.pipeline` calls `nested_locations_within` once per
    table and this function once per file, instead of re-deriving the
    nested set on every file as `is_owned_by_location` alone would.
    """
    location = "file:///root/managed/gold/T-abc"
    nested_location = "file:///root/managed/gold/T-abc/U-def"
    other_locations = [nested_location, "file:///root/managed/gold/customers-ghi"]
    own_file = f"{location}/data/x.parquet"
    nested_file = f"{nested_location}/data/y.parquet"

    nested = nested_locations_within(location, other_locations)
    for uri in (own_file, nested_file):
        assert is_owned_by_location_given_nested(location, uri, nested) == is_owned_by_location(
            location, uri, other_locations
        )
    assert is_owned_by_location_given_nested(location, own_file, nested) is True
    assert is_owned_by_location_given_nested(location, nested_file, nested) is False


@pytest.mark.parametrize(
    "inside", ["artifacts", "artifacts/" + "a" * 32, "artifacts/x/../" + "b" * 32]
)
def test_confine_to_root_reserves_the_artifacts_directory(
    session: Session, environment: Environment, inside: str
) -> None:
    """A table at `<root>/artifacts/...` would be taken for an artifact's directory."""
    resolved = StorageResolver(session).for_environment("dev")
    with pytest.raises(ValidationError, match="reserved for artifacts"):
        resolved.confine_to_root(f"{environment.storage_root}/{inside}")


def test_confine_to_root_does_not_reserve_a_name_that_only_starts_like_it(
    session: Session, environment: Environment
) -> None:
    resolved = StorageResolver(session).for_environment("dev")
    for ok in ("artifacts-archive/t", "my-artifacts/t", "managed/artifacts/t"):
        location = f"{environment.storage_root}/{ok}"
        assert resolved.confine_to_root(location) == location


def _storage_secret(session: Session, use: SecretUse | None) -> EnvKeyProvider:
    """Create environment `dev` with the storage secret `s3`, bound to `use`.

    Parameters
    ----------
    session
        Database session.
    use
        What the secret is bound to; `None` leaves it unbound.

    Returns
    -------
    EnvKeyProvider
        The key provider that encrypted it.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    env = Environment(
        id=uuid.uuid4(), name="dev", storage_root="s3://bucket/dev", storage_secret_ref="s3"
    )
    session.add_all([principal, env])
    session.commit()
    keys = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    SecretService(session, AllowAllAuthorization(), keys).create(
        PrincipalRef(id=principal.id, name=principal.name, type=principal.type),
        environment="dev",
        name="s3",
        values={"access_key_id": "AKIA-NEVER-SHOWN"},
    )
    if use is not None:
        # The target has no row here, as after its removal, so the binding is written directly.
        secret = session.execute(select(Secret)).scalar_one()
        secret.use_kind, secret.use_target_id = use.kind, use.target_id
        session.commit()
    return keys


def test_a_storage_secret_bound_to_an_index_is_refused(session: Session) -> None:
    """The environment's storage reads only an unbound or environment-bound secret."""
    keys = _storage_secret(session, SecretUse(SecretUseKind.PACKAGE_INDEX, uuid.uuid4(), "corp"))

    with pytest.raises(SecretUseRefused) as caught:
        StorageResolver(session, keys).for_environment("dev")

    assert "bound to a package index that was removed" in str(caught.value)
    assert str(caught.value).endswith("lakana secret bind s3 --env dev --own-env")
    assert "AKIA-NEVER-SHOWN" not in str(caught.value)


def test_a_storage_secret_bound_to_a_repository_is_refused(session: Session) -> None:
    """A repository's credential is not the environment's storage key."""
    keys = _storage_secret(session, SecretUse(SecretUseKind.REPOSITORY, uuid.uuid4(), "r"))

    with pytest.raises(SecretUseRefused, match="bound to a repository that was removed"):
        StorageResolver(session, keys).for_environment("dev")


@pytest.mark.parametrize("use", [None, SecretUse.environment()])
def test_an_unbound_or_environment_bound_storage_secret_is_read(
    session: Session, use: SecretUse | None
) -> None:
    """An unbound secret keeps serving storage; so does one bound to its environment."""
    keys = _storage_secret(session, use)

    resolved = StorageResolver(session, keys).for_environment("dev")

    assert resolved.config.options["access_key_id"] == "AKIA-NEVER-SHOWN"


def test_a_leading_double_slash_is_not_canonical() -> None:
    """``posixpath.normpath`` keeps two leading slashes; a location must not."""
    assert not is_within_location("file:///store", "file:////store/managed/x")
    assert not is_within_location("file:////store", "file:////store/managed/x")
    assert is_within_location("file:///store", "file:///store/managed/x")
    assert is_within_location("s3://bucket", "s3://bucket/x")
    assert not is_within_location("s3://bucket", "s3://bucket//x")


def test_a_relative_or_hosted_file_location_is_not_canonical() -> None:
    """Without an absolute path and an empty host, a file location has no one reading."""
    assert not is_within_location("file://store", "file://store/managed/x")
    assert not is_within_location("file://localhost/data", "file://localhost/data/x")


@pytest.mark.parametrize(
    ("root", "base"),
    [
        ("file:///", "file://"),
        ("file:///data/", "file:///data"),
        ("s3://bucket//", "s3://bucket"),
        ("s3://bucket/lake", "s3://bucket/lake"),
        ("no-scheme/", "no-scheme"),
    ],
)
def test_root_base_keeps_the_root_of_a_disk_a_root(root: str, base: str) -> None:
    """Only the path's slashes go: ``file:///`` + ``/managed`` is its ``managed/``."""
    from lakana_core.storage.resolver import root_base

    assert root_base(root) == base
