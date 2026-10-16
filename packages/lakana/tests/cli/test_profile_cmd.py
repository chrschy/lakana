"""Tests for `lakana profile list|use|remove`."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from conftest import FakeApi, invoke, make_state

from lakana.cli import _profiles
from lakana.cli._state import CliState


def _state_with_two_profiles(tmp_path: Path, *, interactive: bool = False) -> CliState:
    """Build a state whose profile file holds profiles `a` (default) and `b`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    interactive
        Whether prompts may be shown.

    Returns
    -------
    CliState
        The state.
    """
    state = make_state(tmp_path, FakeApi(), interactive=interactive)
    _profiles.set_profile(
        state.profile_path(),
        "a",
        url="http://a",
        token="lakana_pat_k_secret",
        default_environment=None,
    )
    _profiles.set_profile(
        state.profile_path(),
        "b",
        url="http://b",
        token="lakana_pat_k_secret",
        default_environment=None,
    )
    return state


def test_profile_list_shows_names_without_tokens(tmp_path: Path) -> None:
    """The listing names every profile and marks the default; tokens never appear."""
    state = _state_with_two_profiles(tmp_path)

    result = invoke(state, "--output", "json", "profile", "list")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"name": "a", "url": "http://a", "default_environment": None, "is_default": True},
        {"name": "b", "url": "http://b", "default_environment": None, "is_default": False},
    ]
    assert "secret" not in result.stdout


def test_profile_use_switches_the_default_and_sets_its_environment(tmp_path: Path) -> None:
    """`profile use b --env dev` makes `b` the default with `dev` as its environment."""
    state = _state_with_two_profiles(tmp_path)

    result = invoke(state, "profile", "use", "b", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    data = tomllib.loads(state.profile_path().read_text())
    assert data["default_profile"] == "b"
    assert data["profiles"]["b"]["default_environment"] == "dev"


def test_profile_use_without_env_only_switches_the_default(tmp_path: Path) -> None:
    """`profile use b` alone changes the default profile without touching its environment."""
    state = _state_with_two_profiles(tmp_path)

    result = invoke(state, "profile", "use", "b")

    assert result.exit_code == 0, result.stderr
    data = tomllib.loads(state.profile_path().read_text())
    assert data["default_profile"] == "b"
    assert "default_environment" not in data["profiles"]["b"]


def test_profile_use_refuses_an_unknown_profile(tmp_path: Path) -> None:
    """Switching to a profile that doesn't exist is a usage error."""
    result = invoke(_state_with_two_profiles(tmp_path), "profile", "use", "zzz")

    assert result.exit_code == 2


def test_profile_remove_asks_first(tmp_path: Path) -> None:
    """`profile remove` refuses without a TTY, and removes with `--yes`."""
    state = _state_with_two_profiles(tmp_path)

    refused = invoke(state, "profile", "remove", "b")
    removed = invoke(state, "profile", "remove", "b", "--yes")

    assert refused.exit_code == 2
    assert removed.exit_code == 0, removed.stderr
    assert "b" not in tomllib.loads(state.profile_path().read_text())["profiles"]


def test_profile_remove_can_be_declined_interactively(tmp_path: Path) -> None:
    """Answering `n` keeps the profile and exits 2."""
    state = _state_with_two_profiles(tmp_path, interactive=True)

    result = invoke(state, "profile", "remove", "b", input="n\n")

    assert result.exit_code == 2
    assert "b" in tomllib.loads(state.profile_path().read_text())["profiles"]
