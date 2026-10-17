"""`lakana index *`."""

from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_BASE = "/api/v1/package-indexes"
_INDEX = {
    "id": "00000000-0000-0000-0000-000000000001",
    "name": "team",
    "url": "https://pkgs.example.org/simple",
    "credential_secret": "team-index",
    "created_by": None,
    "created_at": "2026-10-07T12:00:00+00:00",
    "updated_at": "2026-10-07T12:00:00+00:00",
}


def _plain(text: str) -> str:
    """Return CLI output without ANSI codes or Rich box borders, whitespace collapsed.

    Parameters
    ----------
    text
        Raw stdout or stderr.

    Returns
    -------
    str
        The plain, single-spaced text.
    """
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for border in "│╭╮╰╯─":
        plain = plain.replace(border, " ")
    return " ".join(plain.split())


def test_index_add_sends_the_fields_and_says_what_to_do_next(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_INDEX)
    result = invoke(
        make_state(tmp_path, api),
        "index",
        "add",
        "team",
        "--url",
        "https://pkgs.example.org/simple",
        "--credential-secret",
        "team-index",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[-1].content) == {
        "name": "team",
        "url": "https://pkgs.example.org/simple",
        "credential_secret": "team-index",
    }
    assert "Registered package index team" in result.stdout
    assert _plain(result.stderr).endswith(
        "Next: create the secret in each environment whose projects use this index: lakana "
        'secret create team-index --env ENV --values-stdin (a JSON object with "username" and '
        '"password").'
        " Then bind it to this index: lakana secret bind SECRET --env ENV --index INDEX."
    )


def test_index_add_quotes_a_secret_name_in_the_hint(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json={**_INDEX, "credential_secret": "a;b"})
    result = invoke(
        make_state(tmp_path, api),
        "index",
        "add",
        "team",
        "--url",
        "https://pkgs.example.org/simple",
        "--credential-secret",
        "a;b",
    )
    assert result.exit_code == 0, result.output
    assert "lakana secret create 'a;b' --env ENV" in _plain(result.stderr)


def _add_with_secret(tmp_path: Path, secret: str) -> str:
    """Run `index add` with a credential secret and return what it wrote to stderr.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    secret
        The secret's name.

    Returns
    -------
    str
        Standard error without styling.
    """
    api = FakeApi()
    api.on("POST", _BASE, status=201, json={**_INDEX, "credential_secret": secret})
    result = invoke(
        make_state(tmp_path, api),
        "index",
        "add",
        "team",
        "--url",
        "https://pkgs.example.org/simple",
        "--credential-secret",
        secret,
    )
    assert result.exit_code == 0, result.output
    return _plain(result.stderr)


def test_index_add_shows_a_command_only_when_it_is_complete(tmp_path: Path) -> None:
    """A name that needs quoting and is too long is not cut inside its quotes."""
    text = _add_with_secret(tmp_path, "a;" + "b" * 220)
    assert text.endswith(
        "lakana secret create NAME --env ENV --values-stdin (NAME is the name you gave, too long "
        'to show here) (a JSON object with "username" and "password").'
        " Then bind it to this index: lakana secret bind SECRET --env ENV --index INDEX."
    )
    assert "'" not in text and "…" not in text


def test_index_add_shows_a_quoted_name_of_the_largest_size_whole(tmp_path: Path) -> None:
    secret = "a;" + "b" * 116  # 120 characters with its quotes: the longest that is shown
    assert f"lakana secret create '{secret}' --env ENV" in _add_with_secret(tmp_path, secret)


def test_index_add_without_a_secret_gives_no_hint_and_json_stays_json(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json={**_INDEX, "credential_secret": None})
    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "index",
        "add",
        "team",
        "--url",
        "https://pkgs.example.org/simple",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["name"] == "team"
    assert result.stderr == ""
    assert json.loads(api.requests[-1].content) == {
        "name": "team",
        "url": "https://pkgs.example.org/simple",
        "credential_secret": None,
    }


def test_index_add_with_a_secret_keeps_json_stdout_one_document(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_INDEX)
    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "index",
        "add",
        "team",
        "--url",
        "https://pkgs.example.org/simple",
        "--credential-secret",
        "team-index",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["credential_secret"] == "team-index"


def test_index_list_and_show(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json=[_INDEX])
    api.on("GET", f"{_BASE}/team", json=_INDEX)
    state = make_state(tmp_path, api)
    env = {"GITHUB_ACTIONS": "true"}
    listed = _plain(invoke(state, "index", "list", env=env).stdout)
    assert "team" in listed and "https://pkgs.example.org/simple" in listed
    assert "team-index" in listed
    shown = _plain(invoke(state, "index", "show", "team", env=env).stdout)
    assert "https://pkgs.example.org/simple" in shown and "team-index" in shown


def test_index_update_sends_only_what_was_given_and_clears(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("PATCH", f"{_BASE}/team", json=_INDEX)
    state = make_state(tmp_path, api)
    result = invoke(state, "index", "update", "team", "--clear", "credential_secret")
    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[-1].content) == {"credential_secret": None}
    assert "Updated package index team" in result.stdout
    result = invoke(state, "index", "update", "team", "--url", "https://new.example.org/s")
    assert json.loads(api.requests[-1].content) == {"url": "https://new.example.org/s"}
    result = invoke(state, "index", "update", "team", "--credential-secret", "other")
    assert json.loads(api.requests[-1].content) == {"credential_secret": "other"}


def test_index_update_usage_errors(tmp_path: Path) -> None:
    api = FakeApi()
    state = make_state(tmp_path, api)
    cases = (
        (("--clear", "x"), "--clear accepts credential_secret; got 'x'"),
        (
            ("--credential-secret", "s", "--clear", "credential_secret"),
            "Can't both set and clear credential_secret; pass only one.",
        ),
        ((), "Nothing to change; pass at least one option."),
    )
    for args, text in cases:
        result = invoke(state, "index", "update", "team", *args)
        assert result.exit_code == 2, result.output
        assert text in _plain(result.output)
    assert api.requests == []


def test_index_remove_needs_confirmation(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("DELETE", f"{_BASE}/team", status=204)
    state = make_state(tmp_path, api)
    refused = invoke(state, "index", "remove", "team")
    assert refused.exit_code == 2
    assert api.requests == []
    result = invoke(state, "index", "remove", "team", "--yes")
    assert result.exit_code == 0, result.output
    assert "Deleted package index team" in result.stdout


_HOSTILE = "a\x1b[2Jb\nforged line"


def test_a_name_with_control_characters_is_shown_escaped_in_every_message(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json={**_INDEX, "credential_secret": _HOSTILE})
    api.on("PATCH", f"{_BASE}/{_HOSTILE}", json=_INDEX)
    api.on("DELETE", f"{_BASE}/{_HOSTILE}", status=204)
    state = make_state(tmp_path, api)
    added = invoke(
        state, "index", "add", _HOSTILE, "--url", "https://x.example.org/s",
        "--credential-secret", _HOSTILE,
    )  # fmt: skip
    updated = invoke(state, "index", "update", _HOSTILE, "--clear", "credential_secret")
    removed = invoke(state, "index", "remove", _HOSTILE, "--yes")
    refused = invoke(state, "index", "remove", _HOSTILE)
    for result in (added, updated, removed, refused):
        text = result.stdout + result.stderr + result.output
        assert "\x1b" not in text, text
        assert "forged line" not in text.replace("\\nforged line", "")
    assert "a\\x1b[2Jb\\nforged line" in added.stdout
    assert "a\\x1b[2Jb\\nforged line" in updated.stdout
    assert "a\\x1b[2Jb\\nforged line" in removed.stdout
    assert "\\x1b" in added.stderr and "\\x1b" in refused.output
