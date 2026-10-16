"""Tests for configuration resolution: explicit args, env, then the profile file."""

import errno
import os
from pathlib import Path

import pytest

from lakana._config import ResolvedConfig, default_config_path, resolve_config
from lakana.errors import ConfigError


def _write_profile(path: Path, text: str, mode: int = 0o600) -> Path:
    """Write a profile file with the given permission bits.

    Parameters
    ----------
    path
        Where to write.
    text
        TOML content.
    mode
        File mode.

    Returns
    -------
    Path
        The written path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def test_explicit_arguments_win(tmp_path: Path) -> None:
    """Explicit url/token beat environment variables and the profile file."""
    cfg = _write_profile(
        tmp_path / "c.toml",
        'default_profile = "a"\n[profiles.a]\nurl = "http://file"\ntoken = "lakana_pat_f_s"\n',
    )
    env = {"LAKANA_URL": "http://env", "LAKANA_TOKEN": "lakana_pat_e_s"}
    got = resolve_config("http://arg", "lakana_pat_a_s", environ=env, config_path=cfg)
    assert (got.url, got.token) == ("http://arg", "lakana_pat_a_s")


def test_environment_beats_profile(tmp_path: Path) -> None:
    """LAKANA_URL/LAKANA_TOKEN beat the profile file."""
    cfg = _write_profile(
        tmp_path / "c.toml",
        'default_profile = "a"\n[profiles.a]\nurl = "http://file"\ntoken = "lakana_pat_f_s"\n',
    )
    got = resolve_config(
        environ={"LAKANA_URL": "http://env", "LAKANA_TOKEN": "lakana_pat_e_s"}, config_path=cfg
    )
    assert (got.url, got.token) == ("http://env", "lakana_pat_e_s")


def test_profile_selected_by_env_var(tmp_path: Path) -> None:
    """LAKANA_PROFILE picks a non-default profile."""
    cfg = _write_profile(
        tmp_path / "c.toml",
        'default_profile = "a"\n[profiles.a]\nurl = "http://a"\ntoken = "lakana_pat_a_s"\n'
        '[profiles.b]\nurl = "http://b"\ntoken = "lakana_pat_b_s"\n',
    )
    got = resolve_config(environ={"LAKANA_PROFILE": "b"}, config_path=cfg)
    assert got.url == "http://b"


def test_explicit_profile_argument_beats_env_and_default(tmp_path: Path) -> None:
    """The explicit `profile` argument beats LAKANA_PROFILE and default_profile."""
    cfg = _write_profile(
        tmp_path / "c.toml",
        'default_profile = "a"\n[profiles.a]\nurl = "http://a"\ntoken = "lakana_pat_a_s"\n'
        '[profiles.b]\nurl = "http://b"\ntoken = "lakana_pat_b_s"\n',
    )
    got = resolve_config(profile="b", environ={"LAKANA_PROFILE": "a"}, config_path=cfg)
    assert got.url == "http://b"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permission bits only")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_profile_readable_by_others_is_refused(tmp_path: Path, mode: int) -> None:
    """A profile file accessible to group or others is refused with the fix in the message."""
    cfg = _write_profile(
        tmp_path / "c.toml",
        'default_profile = "a"\n[profiles.a]\nurl = "http://a"\ntoken = "lakana_pat_a_s"\n',
        mode,
    )
    with pytest.raises(ConfigError, match="chmod 600"):
        resolve_config(environ={}, config_path=cfg)


def test_malformed_profile_file_is_a_clear_error(tmp_path: Path) -> None:
    """A malformed or empty profile file names the file; no raw TOML traceback."""
    cfg = _write_profile(tmp_path / "c.toml", "this is = = not toml")
    with pytest.raises(ConfigError, match=str(cfg)):
        resolve_config(environ={}, config_path=cfg)


def test_nothing_resolved_lists_the_sources(tmp_path: Path) -> None:
    """With no url/token anywhere, the error names all three sources."""
    with pytest.raises(ConfigError, match="LAKANA_URL") as excinfo:
        resolve_config(environ={}, config_path=tmp_path / "missing.toml")
    assert "config.toml" in str(excinfo.value)


def test_default_path_respects_xdg(tmp_path: Path) -> None:
    """XDG_CONFIG_HOME is honoured."""
    expected = tmp_path / "lakana" / "config.toml"
    assert default_config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == expected


def test_default_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no XDG_CONFIG_HOME, the default path is under `~/.config`."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_config_path({}) == tmp_path / ".config" / "lakana" / "config.toml"


def test_url_without_token_is_an_error() -> None:
    """A URL given with no token is a partial source, not a merge candidate."""
    with pytest.raises(ConfigError, match="url and token"):
        resolve_config(url="http://arg", environ={}, config_path=Path("/nonexistent"))


def test_token_without_url_is_an_error() -> None:
    """A token given with no URL is a partial source, not a merge candidate."""
    with pytest.raises(ConfigError, match="url and token"):
        resolve_config(token="lakana_pat_a_s", environ={}, config_path=Path("/nonexistent"))


def test_env_url_without_env_token_is_an_error() -> None:
    """LAKANA_URL alone is a partial source."""
    with pytest.raises(ConfigError, match="LAKANA_URL and LAKANA_TOKEN"):
        resolve_config(environ={"LAKANA_URL": "http://env"}, config_path=Path("/nonexistent"))


def test_env_token_without_env_url_is_an_error() -> None:
    """LAKANA_TOKEN alone is a partial source."""
    with pytest.raises(ConfigError, match="LAKANA_URL and LAKANA_TOKEN"):
        resolve_config(environ={"LAKANA_TOKEN": "lakana_pat_e_s"}, config_path=Path("/nonexistent"))


def test_unknown_profile_name_is_an_error(tmp_path: Path) -> None:
    """Selecting a profile the file doesn't define is an error naming the file."""
    cfg = _write_profile(
        tmp_path / "c.toml",
        'default_profile = "a"\n[profiles.a]\nurl = "http://a"\ntoken = "lakana_pat_a_s"\n',
    )
    with pytest.raises(ConfigError, match=str(cfg)):
        resolve_config(profile="missing", environ={}, config_path=cfg)


def test_profile_missing_a_key_is_an_error(tmp_path: Path) -> None:
    """A profile with a url but no token is refused, not merged with anything."""
    cfg = _write_profile(
        tmp_path / "c.toml",
        'default_profile = "a"\n[profiles.a]\nurl = "http://a"\n',
    )
    with pytest.raises(ConfigError, match="url and token"):
        resolve_config(environ={}, config_path=cfg)


def test_no_profile_selected_and_no_default_is_an_error(tmp_path: Path) -> None:
    """With no default_profile and no explicit selection, resolution fails clearly."""
    cfg = _write_profile(tmp_path / "c.toml", "")
    with pytest.raises(ConfigError, match=str(cfg)):
        resolve_config(environ={}, config_path=cfg)


def test_resolved_config_is_frozen(tmp_path: Path) -> None:
    """`ResolvedConfig` is an immutable dataclass."""
    got = resolve_config(
        "http://arg", "lakana_pat_a_s", environ={}, config_path=tmp_path / "missing.toml"
    )
    with pytest.raises(AttributeError):
        got.url = "http://other"  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_profile_with_safe_permissions_is_accepted(tmp_path: Path, mode: int) -> None:
    """Owner-only permission bits (0600 or stricter) are accepted."""
    cfg = _write_profile(
        tmp_path / "c.toml",
        'default_profile = "a"\n[profiles.a]\nurl = "http://a"\ntoken = "lakana_pat_a_s"\n',
        mode,
    )
    got = resolve_config(environ={}, config_path=cfg)
    assert got.url == "http://a"
    cfg.chmod(0o600)  # restore so tmp_path cleanup can remove it


def test_permission_check_is_skipped_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On Windows, the POSIX permission-bit check does not run."""
    cfg = _write_profile(
        tmp_path / "c.toml",
        'default_profile = "a"\n[profiles.a]\nurl = "http://a"\ntoken = "lakana_pat_a_s"\n',
        0o644,
    )
    monkeypatch.setattr(os, "name", "nt")
    got = resolve_config(environ={}, config_path=cfg)
    assert got.url == "http://a"


def test_resolved_config_repr_masks_the_token() -> None:
    """`ResolvedConfig`'s repr never includes the token's secret part."""
    cfg = ResolvedConfig(url="http://x", token="lakana_pat_abc123_x_y-z_secret")
    text = repr(cfg)
    assert "lakana_pat_abc123_***" in text
    assert "x_y-z_secret" not in text
    assert "http://x" in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks/O_NOFOLLOW only")
def test_symlinked_profile_is_refused(tmp_path: Path) -> None:
    """A symlinked profile file is refused outright, never silently followed.

    Reading happens through a single `os.open(..., O_NOFOLLOW)` + `os.fstat`
    on that same descriptor, closing the TOCTOU window between checking a
    path and reading it: a symlink is refused by the open() call itself,
    before any permission check or read.
    """
    real = _write_profile(
        tmp_path / "real.toml",
        'default_profile = "a"\n[profiles.a]\nurl = "http://a"\ntoken = "lakana_pat_a_s"\n',
    )
    link = tmp_path / "link.toml"
    link.symlink_to(real)
    with pytest.raises(ConfigError, match=str(link)):
        resolve_config(environ={}, config_path=link)


def test_missing_profile_file_is_still_skipped(tmp_path: Path) -> None:
    """A profile file that does not exist is skipped, not an error by itself."""
    from lakana._config import _resolve_from_file

    assert _resolve_from_file(tmp_path / "does-not-exist.toml", None, {}) is None


def test_other_os_errors_opening_the_profile_propagate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-symlink OSError opening the profile file propagates unchanged.

    Only the specific `O_NOFOLLOW` symlink rejection (`ELOOP`) is translated
    to `ConfigError`; anything else opening the file (e.g. a permissions
    error unrelated to the group/other-readable check) is not swallowed.
    """
    from lakana._config import _read_profile_file

    def _raise_open(*_args: object, **_kwargs: object) -> int:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("os.open", _raise_open)
    with pytest.raises(PermissionError):
        _read_profile_file(tmp_path / "c.toml")


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ('profiles = "x"\n', "profiles"),
        ('default_profile = "a"\n[profiles]\na = 1\n', "profiles.a"),
        ('default_profile = "a"\n[profiles.a]\nurl = 5\ntoken = "t"\n', "profiles.a.url"),
        ('default_profile = 7\n[profiles.a]\nurl = "u"\ntoken = "t"\n', "default_profile"),
    ],
)
def test_a_profile_file_of_the_wrong_shape_is_a_config_error(
    tmp_path: Path, text: str, where: str
) -> None:
    """Non-table profiles, or non-string values, raise ConfigError naming the key."""
    cfg = _write_profile(tmp_path / "config.toml", text)

    with pytest.raises(ConfigError, match=rf"{where}\b"):
        resolve_config(config_path=cfg, profile="a", environ={})
