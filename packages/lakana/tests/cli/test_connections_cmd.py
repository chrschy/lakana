"""`lakana connection *`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from conftest import FakeApi, invoke, make_state

_BASE = "/api/v1/environments/prod/connections"
_VALUE = "s3cr3t-value"
_CONNECTION: dict[str, Any] = {
    "name": "crm-db",
    "kind": "postgresql",
    "address": {"host": "db.example.com", "port": 5432, "database": "crm"},
    "settings": {"user": "reader", "tls": "verify-full"},
    "secret": "crm-db-connection",
    "declared_by": [{"repository": "analytics", "asset": "customers"}],
    "created_by": "dana",
    "created_at": "2026-10-23T12:00:00+00:00",
    "updated_at": "2026-10-23T12:00:00+00:00",
}
_S3: dict[str, Any] = {
    **_CONNECTION,
    "name": "landing",
    "kind": "s3",
    "address": {
        "bucket": "landing",
        "region": "eu-central-1",
        "endpoint": "https://s3.eu-central-1.amazonaws.com",
    },
    "settings": {"access_key_id": "AKIA1"},
    "secret": "landing-connection",
    "declared_by": [],
}


def _sent(api: FakeApi, method: str) -> list[dict[str, Any]]:
    """Read the JSON bodies of the requests of one method.

    Parameters
    ----------
    api
        The fake.
    method
        The HTTP method.

    Returns
    -------
    list of dict
        The bodies, in order.
    """
    return [json.loads(r.content) for r in api.requests if r.method == method]


def test_create_with_password_stdin(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_CONNECTION)

    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "crm-db",
        "--env",
        "prod",
        "--kind",
        "postgresql",
        "--host",
        "db.example.com",
        "--database",
        "crm",
        "--user",
        "reader",
        "--password-stdin",
        input=f"{_VALUE}\n",
    )

    assert result.exit_code == 0, result.output
    assert _sent(api, "POST") == [
        {
            "name": "crm-db",
            "kind": "postgresql",
            "address": {"host": "db.example.com", "database": "crm"},
            "settings": {"user": "reader"},
            "secret": None,
            "secret_values": {"password": _VALUE},
        }
    ]
    assert "Created connection crm-db in prod with its secret crm-db-connection" in result.stdout
    assert _VALUE not in result.output


def test_password_stdin_strips_one_newline_and_keeps_the_rest(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_CONNECTION)
    invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "crm-db",
        "--env",
        "prod",
        "--kind",
        "tcp",
        "--host",
        "db.example.com",
        "--port",
        "1521",
        "--password-stdin",
        input="pw \n\n",
    )
    (body,) = _sent(api, "POST")
    assert body["address"] == {"host": "db.example.com", "port": 1521}
    assert body["secret_values"] == {"password": "pw \n"}


def test_password_stdin_uses_the_key_of_the_http_auth(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_CONNECTION)
    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "api",
        "--env",
        "prod",
        "--kind",
        "http",
        "--url",
        "https://api.example.com/v1",
        "--auth",
        "bearer",
        "--password-stdin",
        input="tok",
    )
    assert result.exit_code == 0, result.output
    (body,) = _sent(api, "POST")
    assert body["address"] == {"base_url": "https://api.example.com/v1"}
    assert body["settings"] == {"auth": "bearer"}
    assert body["secret_values"] == {"token": "tok"}


def test_password_stdin_for_settings_without_a_secret_is_refused(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "api",
        "--env",
        "prod",
        "--kind",
        "http",
        "--url",
        "https://api.example.com",
        "--password-stdin",
        input="tok",
    )
    assert result.exit_code == 2
    assert (
        "--password-stdin: kind http with auth none takes no secret; give --auth, or leave "
        "out --password-stdin" in result.output
    )
    assert api.requests == []


def test_create_with_values_stdin(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_S3)

    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "landing",
        "--env",
        "prod",
        "--kind",
        "s3",
        "--bucket",
        "landing",
        "--region",
        "eu-central-1",
        "--access-key-id",
        "AKIA1",
        "--values-stdin",
        input='{"secret_access_key": "x", "session_token": "y"}',
    )

    assert result.exit_code == 0, result.output
    (body,) = _sent(api, "POST")
    assert body["address"] == {"bucket": "landing", "region": "eu-central-1"}
    assert body["settings"] == {"access_key_id": "AKIA1"}
    assert body["secret_values"] == {"secret_access_key": "x", "session_token": "y"}
    bad = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "landing",
        "--env",
        "prod",
        "--kind",
        "s3",
        "--bucket",
        "landing",
        "--values-stdin",
        input='{"secret_access_key": 1}',
    )
    assert bad.exit_code == 2
    assert "--values-stdin expects a JSON object of string keys and values" in bad.output


def test_an_option_the_kind_does_not_take_is_refused(tmp_path: Path) -> None:
    api = FakeApi()

    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "landing",
        "--env",
        "prod",
        "--kind",
        "s3",
        "--host",
        "x",
    )
    files = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "exports",
        "--env",
        "prod",
        "--kind",
        "directory",
        "--root",
        "shared",
        "--path",
        "exports",
        "--password-stdin",
        input="pw",
    )

    assert result.exit_code == 2
    assert (
        "--host does not apply to kind s3; its options are --bucket, --region, --endpoint, "
        "--access-key-id, --prefix, --secret, --password-stdin, --values-stdin" in result.output
    )
    assert files.exit_code == 2
    assert (
        "--password-stdin does not apply to kind directory; its options are --root, --path"
        in files.output
    )
    assert api.requests == []


def test_an_unknown_kind_names_the_kinds(tmp_path: Path) -> None:
    result = invoke(
        make_state(tmp_path, FakeApi()),
        "connection",
        "create",
        "x",
        "--env",
        "prod",
        "--kind",
        "oracle",
    )
    assert result.exit_code == 2
    assert "postgresql" in result.output
    assert "tcp" in result.output


def test_two_secret_options_are_refused(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "crm-db",
        "--env",
        "prod",
        "--kind",
        "postgresql",
        "--host",
        "h",
        "--secret",
        "crm-pw",
        "--password-stdin",
        input="pw",
    )
    assert result.exit_code == 2
    assert "Pass only one of --secret, --password-stdin or --values-stdin" in result.output
    assert api.requests == []


def test_ca_file_is_read_into_the_settings(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_CONNECTION)
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    (tmp_path / "ca.pem").write_text(pem)
    args = [
        "connection",
        "create",
        "crm-db",
        "--env",
        "prod",
        "--kind",
        "postgresql",
        "--host",
        "db.example.com",
        "--database",
        "crm",
        "--user",
        "reader",
    ]

    result = invoke(make_state(tmp_path, api), *args, "--ca-file", str(tmp_path / "ca.pem"))
    missing = invoke(make_state(tmp_path, api), *args, "--ca-file", "ca.pem")

    assert result.exit_code == 0, result.output
    assert _sent(api, "POST")[0]["settings"] == {"user": "reader", "ca_certificate": pem}
    assert missing.exit_code == 2
    assert "cannot read --ca-file ca.pem: No such file or directory" in missing.output
    assert len(api.requests) == 1


def test_show_prints_the_connection(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/crm-db", json=_CONNECTION)

    result = invoke(make_state(tmp_path, api), "connection", "show", "crm-db", "--env", "prod")
    as_json = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "connection",
        "show",
        "crm-db",
        "--env",
        "prod",
    )

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert "Connection:  crm-db (postgresql)" in lines
    assert "Address:     host=db.example.com port=5432 database=crm" in lines
    assert "Settings:    user=reader tls=verify-full" in lines
    assert "Secret:      crm-db-connection" in lines
    assert "Declared by: analytics: customers" in lines
    assert json.loads(as_json.stdout) == _CONNECTION


def test_list_prints_one_row_per_connection(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json={"items": [_CONNECTION, _S3], "next_cursor": None})
    result = invoke(make_state(tmp_path, api), "connection", "list", "--env", "prod")
    assert result.exit_code == 0, result.output
    assert "crm-db" in result.stdout
    assert "landing-connection" in result.stdout
    assert "host=db.example.com port=5432 database=crm" in result.stdout


def test_list_follows_every_page(tmp_path: Path) -> None:
    pages = {
        None: {"items": [_CONNECTION], "next_cursor": "c1"},
        "c1": {"items": [_S3], "next_cursor": None},
    }
    api = FakeApi()
    api.routes["GET", _BASE] = lambda request: httpx.Response(
        200, json=pages[request.url.params.get("cursor")]
    )
    result = invoke(
        make_state(tmp_path, api), "--output", "json", "connection", "list", "--env", "prod"
    )
    assert [c["name"] for c in json.loads(result.stdout)] == ["crm-db", "landing"]


def test_update_rotates_the_secret_and_clears_a_setting(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/crm-db", json=_CONNECTION)
    api.on("PATCH", f"{_BASE}/crm-db", json=_CONNECTION)

    rotated = invoke(
        make_state(tmp_path, api),
        "connection",
        "update",
        "crm-db",
        "--env",
        "prod",
        "--host",
        "db2.example.com",
        "--password-stdin",
        input=f"{_VALUE}\n",
    )
    cleared = invoke(
        make_state(tmp_path, api),
        "connection",
        "update",
        "crm-db",
        "--env",
        "prod",
        "--clear",
        "secret",
        "--clear",
        "ca_certificate",
    )
    wrong = invoke(
        make_state(tmp_path, api),
        "connection",
        "update",
        "crm-db",
        "--env",
        "prod",
        "--bucket",
        "b",
    )
    nothing = invoke(make_state(tmp_path, api), "connection", "update", "crm-db", "--env", "prod")

    assert rotated.exit_code == 0, rotated.output
    assert cleared.exit_code == 0, cleared.output
    assert _sent(api, "PATCH") == [
        {"address": {"host": "db2.example.com"}, "secret_values": {"password": _VALUE}},
        {"clear": ["secret", "ca_certificate"]},
    ]
    assert "Updated connection crm-db in prod" in rotated.stdout
    assert _VALUE not in rotated.output
    assert wrong.exit_code == 2
    assert "--bucket does not apply to kind postgresql" in wrong.output
    assert nothing.exit_code == 2
    assert "Nothing to change; pass at least one option." in nothing.output


def test_delete_asks_unless_yes_and_prints_the_409_unchanged(tmp_path: Path) -> None:
    text = (
        "connection `crm-db` is used by `analytics: customers` in `prod`; deploy a version "
        "without it first."
    )
    api = FakeApi()
    api.on("DELETE", f"{_BASE}/crm-db", status=409, json={"detail": text, "type": "Conflict"})

    unasked = invoke(make_state(tmp_path, api), "connection", "delete", "crm-db", "--env", "prod")
    refused = invoke(
        make_state(tmp_path, api), "connection", "delete", "crm-db", "--env", "prod", "--yes"
    )

    assert unasked.exit_code == 2
    assert "Refusing without a terminal; pass --yes to confirm." in unasked.output
    assert refused.exit_code == 7
    assert text in refused.output
    assert len(api.requests) == 1
    api.on("DELETE", f"{_BASE}/crm-db", status=204)
    deleted = invoke(
        make_state(tmp_path, api), "connection", "delete", "crm-db", "--env", "prod", "--yes"
    )
    assert deleted.exit_code == 0, deleted.output
    assert "Deleted connection crm-db in prod" in deleted.stdout
