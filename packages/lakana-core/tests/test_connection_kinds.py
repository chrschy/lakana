"""`lakana_core.connections.kinds`: what each kind accepts, and what a refusal says."""

from __future__ import annotations

from typing import Any

import pytest

from lakana_core.connections.kinds import (
    MAX_CA_BYTES,
    ConnectionKind,
    check_secret_values,
    fingerprint,
    moves_secret,
    secret_keys,
    single_secret_key,
    validate_connection,
)
from lakana_core.errors import ValidationError

K = ConnectionKind

VALID: list[tuple[ConnectionKind, dict[str, Any], dict[str, Any], dict[str, Any]]] = [
    (
        K.POSTGRESQL,
        {"host": "db.example.com", "database": "crm"},
        {"user": "reader"},
        {"host": "db.example.com", "port": 5432, "database": "crm"},
    ),
    (
        K.MARIADB,
        {"host": "10.0.0.5", "port": 3307, "database": "shop"},
        {"user": "r", "tls": "require"},
        {"host": "10.0.0.5", "port": 3307, "database": "shop"},
    ),
    (
        K.SQLITE_FILE,
        {"root": "shared", "path": "exports/app.db"},
        {},
        {"root": "shared", "path": "exports/app.db"},
    ),
    (
        K.HTTP,
        {"base_url": "https://api.example.com/v2"},
        {"auth": "bearer"},
        {"base_url": "https://api.example.com/v2"},
    ),
    (
        K.S3,
        {"bucket": "raw-data", "region": "eu-central-1"},
        {"access_key_id": "AKIA1"},
        {
            "bucket": "raw-data",
            "region": "eu-central-1",
            "endpoint": "https://s3.eu-central-1.amazonaws.com",
        },
    ),
    (K.GCS, {"bucket": "raw_data"}, {"prefix": "exports/"}, {"bucket": "raw_data"}),
    (
        K.AZURE,
        {"account": "acme01", "container": "exports"},
        {},
        {"account": "acme01", "container": "exports"},
    ),
    (K.DIRECTORY, {"root": "shared", "path": "."}, {}, {"root": "shared", "path": "."}),
    (
        K.TCP,
        {"host": "erp.example.com", "port": 1433},
        {},
        {"host": "erp.example.com", "port": 1433},
    ),
]


@pytest.mark.parametrize(("kind", "address", "settings", "normalized"), VALID)
def test_each_kind_accepts_its_example_and_fills_defaults(
    kind: ConnectionKind,
    address: dict[str, Any],
    settings: dict[str, Any],
    normalized: dict[str, Any],
) -> None:
    """A valid address comes back with its defaults filled in."""
    assert validate_connection(kind, address, settings)[0] == normalized


def test_settings_come_back_with_defaults_and_without_nones() -> None:
    """`tls` defaults to the strictest mode, an API key to a header, and `None`s are dropped."""
    _, database = validate_connection(
        K.POSTGRESQL, {"host": "db", "database": "x"}, {"user": "r", "ca_certificate": None}
    )
    assert database == {"user": "r", "tls": "verify-full"}
    _, http = validate_connection(
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "api_key", "api_key_name": "X-Key"},
    )
    assert http == {"auth": "api_key", "api_key_name": "X-Key", "api_key_in": "header"}


def test_a_kind_given_as_its_value_is_the_kind() -> None:
    """The REST value ``s3`` behaves as `ConnectionKind.S3`: the endpoint is still filled."""
    address, _ = validate_connection(
        "s3",  # ty: ignore[invalid-argument-type]
        {"bucket": "raw-data", "region": "eu-central-1"},
        {"access_key_id": "a"},
    )
    assert address["endpoint"] == "https://s3.eu-central-1.amazonaws.com"


def test_an_unknown_kind_is_a_validation_error() -> None:
    """A kind Lakana does not know is refused naming the kinds it does."""
    with pytest.raises(ValidationError, match="postgresql, mariadb"):
        validate_connection("oracle", {}, {})  # ty: ignore[invalid-argument-type]


INVALID: list[tuple[ConnectionKind, dict[str, Any], dict[str, Any], str]] = [
    (K.POSTGRESQL, {"host": "DB.example.com", "database": "x"}, {"user": "r"}, "address.host"),
    (K.POSTGRESQL, {"host": "db.", "database": "x"}, {"user": "r"}, "address.host"),
    (K.POSTGRESQL, {"host": "db", "port": 0, "database": "x"}, {"user": "r"}, "address.port"),
    (K.POSTGRESQL, {"host": "db", "port": 65536, "database": "x"}, {"user": "r"}, "address.port"),
    (K.POSTGRESQL, {"host": "db", "port": True, "database": "x"}, {"user": "r"}, "address.port"),
    (K.POSTGRESQL, {"host": "db", "database": "x"}, {}, "settings.user"),
    (K.POSTGRESQL, {"host": "db", "database": "x\n"}, {"user": "r"}, "address.database"),
    (K.POSTGRESQL, {"host": "db", "database": "x"}, {"user": "r\x00"}, "settings.user"),
    (
        K.POSTGRESQL,
        {"host": "db", "database": "x", "sslmode": "off"},
        {"user": "r"},
        "address.sslmode",
    ),
    (K.POSTGRESQL, {"host": "db", "database": "x"}, {"user": "r", "tls": "prefer"}, "settings.tls"),
    (
        K.POSTGRESQL,
        {"host": "db", "database": "x"},
        {"user": "r", "ca_certificate": "not a pem"},
        "settings.ca_certificate",
    ),
    (K.HTTP, {"base_url": "http://api.example.com"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://user:pw@api.example.com"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://api.example.com/?key=1"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://api.example.com/?"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://api.example.com/#x"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://API.example.com"}, {}, "address.base_url"),
    # urlsplit drops tabs and newlines and strips leading blanks: a client might not.
    (K.HTTP, {"base_url": "https://api.exa\nmple.com"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://api.exa\tmple.evil.com"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": " https://api.example.com"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://api.example.com/a b"}, {}, "address.base_url"),
    # str.isdigit accepts non-ASCII digits; only 1 to 5 ASCII digits are a port.
    (K.HTTP, {"base_url": "https://api.example.com:٤٤٣"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://api.example.com:/"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://api.example.com:0"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://api.example.com:65536"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://[::1]/"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://api.example.com\\@evil.com/"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https:///v1"}, {}, "address.base_url"),
    (K.HTTP, {"base_url": "https://api.example.com"}, {"auth": "basic"}, "settings.username"),
    (
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "basic", "username": "a:b"},
        "settings.username",
    ),
    (K.HTTP, {"base_url": "https://api.example.com"}, {"auth": "api_key"}, "settings.api_key_name"),
    (
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "api_key", "api_key_name": "X-Key\r\nHost: evil.com"},
        "settings.api_key_name",
    ),
    (
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "oauth2", "client_id": "c"},
        "settings.token_url",
    ),
    (
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "oauth2", "client_id": "c", "token_url": "http://id.example.com/token"},
        "settings.token_url",
    ),
    (
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "oauth2", "client_id": "c\n", "token_url": "https://id.example.com/token"},
        "settings.client_id",
    ),
    (
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "bearer", "username": "u"},
        "settings.username",
    ),
    (
        K.HTTP,
        {"base_url": "https://api.example.com"},
        {"api_key_in": "query"},
        "settings.api_key_in",
    ),
    (
        K.S3,
        {"bucket": "raw-data", "region": "eu-central-1", "endpoint": "http://minio:9000"},
        {"access_key_id": "a"},
        "address.endpoint",
    ),
    (K.S3, {"bucket": "raw-data", "region": "eu-central-1"}, {}, "settings.access_key_id"),
    (K.S3, {"bucket": "b", "region": "eu-central-1"}, {"access_key_id": "a"}, "address.bucket"),
    # The default endpoint is built from the region, so the region must make a valid host.
    (K.S3, {"bucket": "raw-data", "region": "eu-"}, {"access_key_id": "a"}, "address.region"),
    (K.S3, {"bucket": "raw-data", "region": "eu.x"}, {"access_key_id": "a"}, "address.region"),
    (K.GCS, {"bucket": "raw"}, {"prefix": "/abs"}, "settings.prefix"),
    (K.GCS, {"bucket": "raw"}, {"prefix": "a/../b"}, "settings.prefix"),
    (K.GCS, {"bucket": "Raw-Data"}, {}, "address.bucket"),
    (K.AZURE, {"account": "acme01", "container": "c"}, {}, "address.container"),
    (K.AZURE, {"account": "acme01", "container": "a--b"}, {}, "address.container"),
    (K.AZURE, {"account": "Acme01", "container": "exports"}, {}, "address.account"),
    (K.DIRECTORY, {"root": "Shared", "path": "x"}, {}, "address.root"),
    (K.DIRECTORY, {"root": "shared", "path": "/etc"}, {}, "address.path"),
    (K.DIRECTORY, {"root": "shared", "path": "a/../../b"}, {}, "address.path"),
    (K.DIRECTORY, {"root": "shared", "path": "a\x00b"}, {}, "address.path"),
    (K.DIRECTORY, {"root": "shared", "path": "a\\..\\b"}, {}, "address.path"),
    (K.DIRECTORY, {"root": "shared", "path": ""}, {}, "address.path"),
    (K.DIRECTORY, {"root": "shared", "path": "x" * 1025}, {}, "address.path"),
    (K.SQLITE_FILE, {"root": "shared", "path": ".."}, {}, "address.path"),
    (K.TCP, {"host": "erp.example.com"}, {}, "address.port"),
    (K.TCP, {"host": "erp.example.com", "port": 0}, {}, "address.port"),
    (K.TCP, {"host": "erp.example.com", "port": 1433}, {"tls": "require"}, "settings.tls"),
]


@pytest.mark.parametrize(("kind", "address", "settings", "field"), INVALID)
def test_each_rule_refuses_naming_the_field(
    kind: ConnectionKind, address: dict[str, Any], settings: dict[str, Any], field: str
) -> None:
    """The refusal names the field, so the CLI can point at the option."""
    with pytest.raises(ValidationError, match=field.replace(".", r"\.")):
        validate_connection(kind, address, settings)


def test_an_unknown_field_names_the_kinds_fields() -> None:
    """A typo is an error that lists what the kind takes, before any missing field."""
    with pytest.raises(
        ValidationError,
        match=(
            r"address\.hots is not a field of kind postgresql; "
            r"its fields are host, port, database"
        ),
    ):
        validate_connection(K.POSTGRESQL, {"hots": "db", "database": "x"}, {"user": "r"})


def test_an_unknown_field_with_an_odd_name_is_not_echoed() -> None:
    """A key that is not a field-like name (it may be a pasted value) is not repeated."""
    with pytest.raises(ValidationError) as caught:
        validate_connection(K.GCS, {"bucket": "raw", "s3cr3t value\n": "x"}, {})
    assert "s3cr3t" not in str(caught.value)
    assert "its fields are bucket" in str(caught.value)


def test_a_refusal_never_repeats_the_value() -> None:
    """A value in the wrong place (a password as the user) is not echoed back."""
    with pytest.raises(ValidationError) as caught:
        validate_connection(K.POSTGRESQL, {"host": "db", "database": "x"}, {"user": "hunter2\n"})
    assert "hunter2" not in str(caught.value)


def test_address_and_settings_together_are_bounded() -> None:
    """More than 16 KiB of JSON is refused before any model runs."""
    with pytest.raises(ValidationError, match="16 KiB"):
        validate_connection(K.GCS, {"bucket": "b"}, {"prefix": "p/" * 9000})


def test_a_value_json_cannot_hold_is_a_validation_error() -> None:
    """A Python caller passing something JSON cannot encode gets a refusal, not a crash."""
    with pytest.raises(ValidationError, match="JSON"):
        validate_connection(K.GCS, {"bucket": {1, 2}}, {})


def test_a_certificate_is_accepted(test_ca_pem: str) -> None:
    """A PEM certificate is a CA bundle, and its fingerprint shows it."""
    _, settings = validate_connection(
        K.POSTGRESQL, {"host": "db", "database": "x"}, {"user": "r", "ca_certificate": test_ca_pem}
    )
    assert settings["ca_certificate"] == test_ca_pem
    assert fingerprint(test_ca_pem).startswith("sha256:")
    assert len(fingerprint(test_ca_pem)) == len("sha256:") + 64


def test_a_40_kib_certificate_bundle_is_accepted(test_ca_pem: str) -> None:
    """The CA counts against its own 64 KiB limit, not the 16 KiB of the other values."""
    bundle = test_ca_pem * (40 * 1024 // len(test_ca_pem))
    assert 32 * 1024 < len(bundle) <= 40 * 1024
    _, settings = validate_connection(
        K.HTTP, {"base_url": "https://api.example.com"}, {"ca_certificate": bundle}
    )
    assert settings["ca_certificate"] == bundle


def test_a_certificate_bundle_over_64_kib_is_refused(test_ca_pem: str) -> None:
    """A CA larger than the limit is refused naming the limit."""
    bundle = test_ca_pem * (MAX_CA_BYTES // len(test_ca_pem) + 1)
    assert len(bundle) > MAX_CA_BYTES
    with pytest.raises(ValidationError, match="64 KiB"):
        validate_connection(
            K.POSTGRESQL, {"host": "db", "database": "x"}, {"user": "r", "ca_certificate": bundle}
        )
    with pytest.raises(ValidationError, match="64 KiB"):
        validate_connection(
            K.POSTGRESQL,
            {"host": "db", "database": "x"},
            {"user": "r", "ca_certificate": test_ca_pem * 200},
        )


def test_a_non_text_certificate_still_counts_against_16_kib() -> None:
    """Only a text CA leaves the 16 KiB count; anything else is counted and then refused."""
    with pytest.raises(ValidationError, match="16 KiB"):
        validate_connection(
            K.POSTGRESQL,
            {"host": "db", "database": "x"},
            {"user": "r", "ca_certificate": ["x" * 17000]},
        )


def test_a_bundle_with_a_private_key_is_refused(test_ca_pem: str) -> None:
    """A private key pasted with the CA would be stored as a setting: refused, unrepeated."""
    key = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg\n"
        "-----END PRIVATE KEY-----\n"
    )
    with pytest.raises(ValidationError, match=r"settings\.ca_certificate") as caught:
        validate_connection(
            K.POSTGRESQL,
            {"host": "db", "database": "x"},
            {"user": "r", "ca_certificate": test_ca_pem + key},
        )
    assert "MIGHAgEA" not in str(caught.value)


@pytest.mark.parametrize(
    ("kind", "settings", "key"),
    [
        (K.POSTGRESQL, {"user": "r"}, "password"),
        (K.MARIADB, {"user": "r"}, "password"),
        (K.SQLITE_FILE, {}, None),
        (K.HTTP, {"auth": "none"}, None),
        (K.HTTP, {"auth": "bearer"}, "token"),
        (K.HTTP, {"auth": "basic", "username": "u"}, "password"),
        (K.HTTP, {"auth": "api_key", "api_key_name": "X-Key"}, "api_key"),
        (
            K.HTTP,
            {"auth": "oauth2", "client_id": "c", "token_url": "https://id.example.com/t"},
            "client_secret",
        ),
        (K.S3, {"access_key_id": "a"}, "secret_access_key"),
        (K.GCS, {}, "service_account_json"),
        (K.AZURE, {}, "account_key"),
        (K.DIRECTORY, {}, None),
        (K.TCP, {}, "password"),
    ],
)
def test_the_single_secret_key_of_each_kind(
    kind: ConnectionKind, settings: dict[str, Any], key: str | None
) -> None:
    """`--password-stdin` stores stdin under this key."""
    assert single_secret_key(kind, settings) == key


def test_secret_values_must_fit_the_kind() -> None:
    """Unknown, missing and ambiguous keys are refused by name; never by value."""
    with pytest.raises(ValidationError, match="secret_access_key"):
        check_secret_values(K.S3, {"access_key_id": "a"}, {"session_token": "t"})
    with pytest.raises(ValidationError, match="password is not a secret key of kind s3"):
        check_secret_values(
            K.S3, {"access_key_id": "a"}, {"secret_access_key": "s", "password": "p"}
        )
    with pytest.raises(ValidationError, match="exactly one of account_key, sas_token"):
        check_secret_values(K.AZURE, {}, {"account_key": "k", "sas_token": "t"})
    with pytest.raises(ValidationError, match="exactly one of account_key, sas_token"):
        check_secret_values(K.AZURE, {}, {})
    with pytest.raises(ValidationError, match="takes no secret"):
        check_secret_values(K.DIRECTORY, {}, {"password": "p"})
    with pytest.raises(ValidationError, match="takes no secret"):
        check_secret_values(K.HTTP, {"auth": "none"}, {"token": "p"})
    check_secret_values(
        K.S3, {"access_key_id": "a"}, {"secret_access_key": "s", "session_token": "t"}
    )
    check_secret_values(K.TCP, {}, {"user": "reader", "password": "p"})  # keys are the team's
    with pytest.raises(ValidationError, match="secret keys of a tcp connection"):
        check_secret_values(K.TCP, {}, {"User-Name": "x"})
    with pytest.raises(ValidationError, match="secret keys of a tcp connection"):
        check_secret_values(K.TCP, {}, {})
    with pytest.raises(ValidationError, match="secret keys of a tcp connection"):
        check_secret_values(K.TCP, {}, {f"k{i}": "v" for i in range(17)})
    assert secret_keys(K.S3, {"access_key_id": "a"}) == (
        frozenset({"secret_access_key"}),
        frozenset({"session_token"}),
    )


@pytest.mark.parametrize("value", ["", "x" * (64 * 1024 + 1), 5])
@pytest.mark.parametrize(
    ("kind", "settings", "key"),
    [(K.POSTGRESQL, {"user": "r"}, "password"), (K.TCP, {}, "password")],
)
def test_a_secret_value_is_non_empty_text_of_at_most_64_kib(
    kind: ConnectionKind, settings: dict[str, Any], key: str, value: object
) -> None:
    """An empty, oversized or non-text value is refused naming the key only."""
    with pytest.raises(ValidationError, match=f"secret's {key} must be a non-empty text"):
        check_secret_values(kind, settings, {key: value})  # ty: ignore[invalid-argument-type]


def test_an_unknown_secret_key_with_an_odd_name_is_not_echoed() -> None:
    """A key that looks like a pasted value is refused without repeating it."""
    with pytest.raises(ValidationError) as caught:
        check_secret_values(K.POSTGRESQL, {"user": "r"}, {"password": "p", "Hunter2!": "x"})
    assert "Hunter2" not in str(caught.value)
    assert "its keys are password" in str(caught.value)


PG = {"host": "a", "port": 5432, "database": "x"}
PG_SETTINGS = {"user": "r", "tls": "verify-full"}
OAUTH = {"auth": "oauth2", "client_id": "c", "token_url": "https://id.example.com/t"}
HTTP_ADDRESS = {"base_url": "https://a.example.com"}
KEY_SETTINGS = {"auth": "api_key", "api_key_name": "X-Key", "api_key_in": "header"}


@pytest.mark.parametrize(
    ("kind", "old", "new", "moved"),
    [
        (K.POSTGRESQL, (PG, PG_SETTINGS), ({**PG, "database": "y"}, PG_SETTINGS), False),
        (K.POSTGRESQL, (PG, PG_SETTINGS), ({**PG, "host": "b"}, PG_SETTINGS), True),
        (K.POSTGRESQL, (PG, PG_SETTINGS), ({**PG, "port": 6432}, PG_SETTINGS), True),
        (K.POSTGRESQL, (PG, PG_SETTINGS), (PG, {**PG_SETTINGS, "user": "w"}), False),
        (
            K.HTTP,
            (HTTP_ADDRESS, OAUTH),
            (HTTP_ADDRESS, {**OAUTH, "token_url": "https://evil.example.com/t"}),
            True,
        ),
        (K.HTTP, (HTTP_ADDRESS, OAUTH), ({"base_url": "https://b.example.com"}, OAUTH), True),
        (
            K.HTTP,
            (HTTP_ADDRESS, OAUTH),
            ({"base_url": "https://a.example.com/v2"}, OAUTH),
            True,
        ),
        (K.HTTP, (HTTP_ADDRESS, OAUTH), (HTTP_ADDRESS, {**OAUTH, "scope": "read"}), False),
        (
            K.S3,
            (
                {"bucket": "a", "region": "r", "endpoint": "https://s3.r.amazonaws.com"},
                {"access_key_id": "k"},
            ),
            (
                {"bucket": "b", "region": "r", "endpoint": "https://s3.r.amazonaws.com"},
                {"access_key_id": "k"},
            ),
            True,
        ),
        (
            K.AZURE,
            ({"account": "a", "container": "c"}, {}),
            ({"account": "a", "container": "d"}, {}),
            False,
        ),
        (
            K.AZURE,
            ({"account": "a", "container": "c"}, {}),
            ({"account": "b", "container": "c"}, {}),
            True,
        ),
        (
            K.TCP,
            ({"host": "a", "port": 1433}, {}),
            ({"host": "a", "port": 1434}, {}),
            True,
        ),
    ],
)
def test_which_changes_move_the_secret(
    kind: ConnectionKind, old: tuple, new: tuple, moved: bool
) -> None:
    """Host, port, base_url, endpoint, token_url, bucket and account decide where it goes."""
    assert moves_secret(kind, *old, *new) is moved


@pytest.mark.parametrize(
    ("old_tls", "new_tls", "moved"),
    [
        ("verify-full", "disable", True),
        ("verify-full", "require", True),
        ("require", "disable", True),
        ("disable", "require", False),
        ("require", "verify-full", False),
        ("verify-full", "verify-full", False),
    ],
)
def test_weakening_tls_moves_the_secret(old_tls: str, new_tls: str, moved: bool) -> None:
    """A weaker TLS mode lets someone on the path read the password: it needs re-supplying."""
    assert (
        moves_secret(
            K.POSTGRESQL, PG, {**PG_SETTINGS, "tls": old_tls}, PG, {**PG_SETTINGS, "tls": new_tls}
        )
        is moved
    )


def test_a_missing_tls_counts_as_the_strictest_mode() -> None:
    """Settings without `tls` mean `verify-full`, so dropping it is no weakening."""
    assert moves_secret(K.POSTGRESQL, PG, {"user": "r"}, PG, PG_SETTINGS) is False
    assert moves_secret(K.POSTGRESQL, PG, {"user": "r"}, PG, {**PG_SETTINGS, "tls": "disable"})


@pytest.mark.parametrize(
    ("old_ca", "new_ca"),
    [(None, "ca-one"), ("ca-one", "ca-two"), ("ca-one", None)],
    ids=["set", "changed", "removed"],
)
def test_any_change_of_the_ca_moves_the_secret(old_ca: str | None, new_ca: str | None) -> None:
    """A CA decides whom TLS trusts: a new one could trust an interceptor."""
    old = {**PG_SETTINGS, **({"ca_certificate": old_ca} if old_ca else {})}
    new = {**PG_SETTINGS, **({"ca_certificate": new_ca} if new_ca else {})}
    assert moves_secret(K.POSTGRESQL, PG, old, PG, new) is True
    assert moves_secret(K.HTTP, HTTP_ADDRESS, {**OAUTH, **old}, HTTP_ADDRESS, {**OAUTH, **new})


def test_moving_an_api_key_from_the_header_to_the_query_moves_the_secret() -> None:
    """A key in the query string lands in access logs along the way."""
    assert moves_secret(
        K.HTTP, HTTP_ADDRESS, KEY_SETTINGS, HTTP_ADDRESS, {**KEY_SETTINGS, "api_key_in": "query"}
    )
    assert not moves_secret(
        K.HTTP, HTTP_ADDRESS, KEY_SETTINGS, HTTP_ADDRESS, {**KEY_SETTINGS, "api_key_name": "Y"}
    )


def test_changing_the_auth_moves_the_secret() -> None:
    """A bearer token resent as a basic password, or another auth, is a different use."""
    assert moves_secret(
        K.HTTP,
        HTTP_ADDRESS,
        {"auth": "bearer"},
        HTTP_ADDRESS,
        {"auth": "basic", "username": "u"},
    )
