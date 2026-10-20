"""Tests for `lakana env *`."""

from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_DEV = {
    "name": "dev",
    "storage_root": "file:///data",
    "storage_secret_ref": None,
    "is_protected": False,
    "properties": {},
}


def test_env_create_derives_the_scheme_and_sends_options(tmp_path: Path) -> None:
    """`env create` builds the storage payload from `--storage-root` and `--option`."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)

    result = invoke(
        make_state(tmp_path, api),
        "env",
        "create",
        "dev",
        "--storage-root",
        "s3://bucket/lake",
        "--option",
        "region=eu-west-1",
    )

    assert result.exit_code == 0, result.stderr
    assert "Created environment dev" in result.stdout
    assert json.loads(api.requests[0].content)["storage"] == {
        "scheme": "s3",
        "root": "s3://bucket/lake",
        "options": {"region": "eu-west-1"},
    }


def test_env_create_refuses_a_root_without_a_supported_scheme(tmp_path: Path) -> None:
    """A bare path or an unknown scheme is a usage error, before any request."""
    api = FakeApi()

    bare = invoke(make_state(tmp_path, api), "env", "create", "dev", "--storage-root", "/data")
    odd = invoke(make_state(tmp_path, api), "env", "create", "dev", "--storage-root", "gs://b")

    assert (bare.exit_code, odd.exit_code) == (2, 2)
    assert "file://" in bare.stderr
    assert api.requests == []


def test_env_list_and_get(tmp_path: Path) -> None:
    """`env list` renders a table; `env get` a single environment."""
    api = FakeApi()
    api.on("GET", "/api/v1/environments", json=[_DEV])
    api.on("GET", "/api/v1/environments/dev", json=_DEV)

    listed = invoke(make_state(tmp_path, api), "env", "list")
    got = invoke(make_state(tmp_path, api), "--output", "json", "env", "get", "dev")

    assert listed.exit_code == 0, listed.stderr
    assert "file:///data" in listed.stdout
    assert json.loads(got.stdout) == {
        **_DEV,
        "retention_days": None,
        "deleted_at": None,
        "purge_after": None,
    }


_DELETED = {
    **_DEV,
    "retention_days": None,
    "deleted_at": "2026-10-20T10:00:00+00:00",
    "purge_after": "2026-10-27T10:00:00+00:00",
}


def test_env_delete_requires_confirmation(tmp_path: Path) -> None:
    """Without a TTY, `env delete` refuses unless `--yes` is given."""
    api = FakeApi()
    api.on("DELETE", "/api/v1/environments/dev", json=_DELETED)

    refused = invoke(make_state(tmp_path, api), "env", "delete", "dev")
    deleted = invoke(make_state(tmp_path, api), "env", "delete", "dev", "--yes")

    assert refused.exit_code == 2
    assert deleted.exit_code == 0, deleted.stderr
    assert "lakana env restore dev" in deleted.stdout
    assert "2026-10-27" in deleted.stdout
    assert [r.method for r in api.requests] == ["DELETE"]


def test_env_delete_purge_asks_differently_and_sends_purge(tmp_path: Path) -> None:
    """`--purge` says it cannot be undone, and sends `purge=true`."""
    api = FakeApi()
    api.on("DELETE", "/api/v1/environments/dev", json=_DELETED)

    asked = invoke(make_state(tmp_path, api), "env", "delete", "dev", "--purge")
    deleted = invoke(make_state(tmp_path, api), "env", "delete", "dev", "--purge", "--yes")

    assert asked.exit_code == 2
    assert "cannot be undone" in asked.stderr
    assert deleted.exit_code == 0, deleted.stderr
    assert api.requests[-1].url.params["purge"] == "true"
    assert "cannot be restored" in deleted.stdout


def test_env_delete_against_an_older_server_still_succeeds(tmp_path: Path) -> None:
    """A 204 without a body: the message says nothing about an undo period."""
    api = FakeApi()
    api.on("DELETE", "/api/v1/environments/dev", status=204)

    deleted = invoke(make_state(tmp_path, api), "env", "delete", "dev", "--yes")

    assert deleted.exit_code == 0, deleted.stderr
    assert "Deleted environment dev" in deleted.stdout


def test_env_restore_and_list_deleted(tmp_path: Path) -> None:
    """`env restore` POSTs `/restore`; `env list --deleted` shows until when."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/restore", json={**_DELETED, "deleted_at": None})
    api.on("GET", "/api/v1/environments", json=[_DELETED])

    restored = invoke(make_state(tmp_path, api), "env", "restore", "dev")
    listed = invoke(make_state(tmp_path, api), "env", "list", "--deleted")

    assert restored.exit_code == 0, restored.stderr
    assert "Restored environment dev" in restored.stdout
    assert listed.exit_code == 0, listed.stderr
    assert "purge_after" in listed.stdout and "2026-10-27" in listed.stdout
    assert api.requests[-1].url.params["deleted"] == "true"


def test_env_set_storage_needs_something_to_change(tmp_path: Path) -> None:
    """`set-storage` with neither `--option` nor `--secret-ref` is a usage error."""
    assert invoke(make_state(tmp_path, FakeApi()), "env", "set-storage", "dev").exit_code == 2


def test_env_set_storage_sends_options_and_secret_ref(tmp_path: Path) -> None:
    """`set-storage` forwards both fields to `update_storage`.

    `environments.update_storage` sends `PATCH /api/v1/environments/{name}/storage`
    (see `packages/lakana/src/lakana/_routes.py`), matching the plan's guess.
    """
    api = FakeApi()
    api.on(
        "PATCH", "/api/v1/environments/dev/storage", json={**_DEV, "storage_secret_ref": "creds"}
    )

    result = invoke(
        make_state(tmp_path, api),
        "env",
        "set-storage",
        "dev",
        "--option",
        "a=b",
        "--secret-ref",
        "creds",
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[0].content) == {"options": {"a": "b"}, "secret_ref": "creds"}


def test_env_protect_and_unprotect(tmp_path: Path) -> None:
    """`protect` needs no confirmation; `unprotect` does.

    `environments.set_protection` sends `PATCH /api/v1/environments/{name}`
    with `{"is_protected": ...}` (see `_routes.py`), not the plan's guessed
    `PUT .../protection`.
    """
    api = FakeApi()
    api.on("PATCH", "/api/v1/environments/dev", json={**_DEV, "is_protected": True})

    protect = invoke(make_state(tmp_path, api), "env", "protect", "dev")
    unprotect_refused = invoke(make_state(tmp_path, api), "env", "unprotect", "dev")
    unprotect = invoke(make_state(tmp_path, api), "env", "unprotect", "dev", "--yes")

    assert protect.exit_code == 0, protect.stderr
    assert unprotect_refused.exit_code == 2
    assert unprotect.exit_code == 0, unprotect.stderr
    assert [json.loads(r.content) for r in api.requests] == [
        {"is_protected": True},
        {"is_protected": False},
    ]


def test_env_create_with_a_malformed_option_never_echoes_it(tmp_path: Path) -> None:
    """Final review minor 9: a storage `--option` may hold a credential; never echo it."""
    api = FakeApi()
    state = make_state(tmp_path, api)

    for option in ("AKIAHIDDEN", "=AKIAHIDDEN"):
        result = invoke(
            state, "env", "create", "dev", "--storage-root", "s3://b/l", "--option", option
        )
        assert result.exit_code == 2
        assert "AKIAHIDDEN" not in result.stdout + result.stderr
        assert "--option #1 expects key=value" in result.stderr
    assert api.requests == []


def test_env_set_storage_with_a_malformed_option_never_echoes_it(tmp_path: Path) -> None:
    """Final review minor 9: the same protection applies to `env set-storage`."""
    api = FakeApi()

    result = invoke(
        make_state(tmp_path, api),
        "env",
        "set-storage",
        "dev",
        "--option",
        "a=b",
        "--option",
        "AKIAHIDDEN",
    )

    assert result.exit_code == 2
    assert "AKIAHIDDEN" not in result.stdout + result.stderr
    assert "--option #2 expects key=value" in result.stderr
    assert api.requests == []


def test_env_option_help_warns_about_shell_history(tmp_path: Path) -> None:
    """Final review minor 9: `--option` help steers credentials to `--secret-ref`."""
    for command in ("create", "set-storage"):
        result = invoke(make_state(tmp_path, FakeApi()), "env", command, "--help")
        # CI forces colour and wraps the help box: drop ANSI codes and the box's
        # side borders before joining the wrapped lines.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).replace("│", " ")
        text = " ".join(plain.split())
        assert "Values land in shell history; prefer --secret-ref for credentials." in text


def _help(tmp_path: Path, command: str) -> str:
    """Return `lakana env COMMAND --help` as plain text, whitespace collapsed.

    Parameters
    ----------
    tmp_path
        Profile directory.
    command
        The `env` subcommand.

    Returns
    -------
    str
        The help text.
    """
    result = invoke(make_state(tmp_path, FakeApi()), "env", command, "--help")
    assert result.exit_code == 0, result.stderr
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).replace("│", " ")
    return " ".join(plain.split())


def test_env_protect_help_says_it_refuses_writes_outside_runs(tmp_path: Path) -> None:
    """I2: protection now also locks writes down, not only deletion and storage changes."""
    text = _help(tmp_path, "protect")
    assert "refuses every write outside runs of the deployed code" in text
    assert "--break-glass" in text
    assert "deletion and storage changes" in text


def test_env_delete_help_says_to_undeploy_first(tmp_path: Path) -> None:
    """I2: a deployed repository blocks deletion (409); the help says what to do."""
    text = _help(tmp_path, "delete")
    assert "Refused (409) while any repository is deployed to it" in text
    assert "lakana undeploy REPO --env NAME" in text
    assert "lakana env restore NAME" in text
