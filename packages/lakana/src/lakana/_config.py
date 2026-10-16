"""Resolution of SDK configuration.

Three sources are checked in priority order: explicit constructor arguments,
`LAKANA_URL`/`LAKANA_TOKEN` environment variables, then the profile file. The
first source that names a URL or a token at all must name both, or
resolution fails rather than merging fields from a lower-priority source.
"""

from __future__ import annotations

import errno
import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from lakana._tokens import mask_token
from lakana.errors import ConfigError


@dataclass(frozen=True)
class ResolvedConfig:
    """The URL and token the SDK uses for every request.

    Parameters
    ----------
    url
        The platform API's base URL.
    token
        A PAT or task token, sent as a bearer credential.
    """

    url: str
    token: str = field(repr=False)

    def __repr__(self) -> str:
        """Return a repr with the token masked.

        Returns
        -------
        str
            `"ResolvedConfig(url='...', token='lakana_pat_<key>_***')"`, never
            including the token's secret part.
        """
        return f"ResolvedConfig(url={self.url!r}, token={mask_token(self.token)!r})"


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Compute the default profile file path.

    Parameters
    ----------
    environ
        The environment to read `XDG_CONFIG_HOME` from. Defaults to
        `os.environ`.

    Returns
    -------
    Path
        `$XDG_CONFIG_HOME/lakana/config.toml`, or `~/.config/lakana/config.toml`
        when `XDG_CONFIG_HOME` is unset.
    """
    env = environ if environ is not None else os.environ
    xdg_home = env.get("XDG_CONFIG_HOME")
    base = Path(xdg_home) if xdg_home else Path.home() / ".config"
    return base / "lakana" / "config.toml"


def _read_profile_file(config_path: Path) -> str | None:
    """Read the profile file's text, checking its permissions on the same fd.

    Opens the file once with `O_NOFOLLOW` (where the platform supports it)
    and checks the permission mode via `os.fstat` on that same file
    descriptor before reading from it, so there is no window between
    checking the file and reading it in which it could be swapped for a
    symlink or have its permissions changed (TOCTOU).

    Parameters
    ----------
    config_path
        Path to the profile file.

    Returns
    -------
    str | None
        The file's text, or `None` if it does not exist.

    Raises
    ------
    ConfigError
        If the path is a symlink (detected via `O_NOFOLLOW`, POSIX only), or
        if it is readable by other users (POSIX only; skipped on Windows,
        where neither check applies).
    OSError
        For any other failure opening the file (e.g. a permissions error
        unrelated to the group/other-readable check above), unchanged.
    """
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(config_path, os.O_RDONLY | o_nofollow)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if o_nofollow and exc.errno == errno.ELOOP:
            raise ConfigError(f"{config_path} is a symlink; refusing to read it") from exc
        raise
    with os.fdopen(fd) as fh:
        if os.name != "nt":
            mode = stat.S_IMODE(os.fstat(fh.fileno()).st_mode)
            if mode & 0o077:
                raise ConfigError(
                    f"{config_path} is readable by other users; run: chmod 600 {config_path}"
                )
        return fh.read()


def validate_profile_document(data: Mapping[str, object], config_path: Path) -> None:
    """Check a parsed profile file has the shape the SDK and CLI rely on.

    `default_profile` (if present) is a string; `profiles` (if present) is a
    table whose entries are tables; and each entry's `url`, `token` and
    `default_environment` (if present) are strings.

    Parameters
    ----------
    data
        The parsed TOML document.
    config_path
        The file it came from, for error messages.

    Raises
    ------
    ConfigError
        Naming the first key with the wrong shape.
    """
    default = data.get("default_profile")
    if default is not None and not isinstance(default, str):
        raise ConfigError(f"{config_path}: default_profile must be a string")
    profiles = data.get("profiles", {})
    if not isinstance(profiles, Mapping):
        raise ConfigError(f"{config_path}: profiles must be a table of [profiles.NAME] tables")
    for name, entry in profiles.items():
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{config_path}: profiles.{name} must be a table")
        for key in ("url", "token", "default_environment"):
            value = entry.get(key)
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"{config_path}: profiles.{name}.{key} must be a string")


def _resolve_from_file(
    config_path: Path, profile: str | None, environ: Mapping[str, str]
) -> ResolvedConfig | None:
    """Resolve a URL and token from the profile file, if it exists.

    Parameters
    ----------
    config_path
        Path to the profile file.
    profile
        An explicitly chosen profile name, taking precedence over
        `LAKANA_PROFILE` and the file's own `default_profile`.
    environ
        The environment to read `LAKANA_PROFILE` from.

    Returns
    -------
    ResolvedConfig | None
        The resolved configuration, or `None` if the file does not exist.

    Raises
    ------
    ConfigError
        If the file exists but is a symlink, is readable by other users, is
        not valid TOML, has the wrong shape (`validate_profile_document`),
        names no usable profile, or that profile is missing `url` or `token`.
    """
    text = _read_profile_file(config_path)
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc
    validate_profile_document(data, config_path)
    profile_name = profile or environ.get("LAKANA_PROFILE") or data.get("default_profile")
    if not profile_name:
        raise ConfigError(f"{config_path} names no default_profile, and none was selected")
    profile_data = data.get("profiles", {}).get(profile_name)
    if profile_data is None:
        raise ConfigError(f"{config_path} has no profile named {profile_name!r}")
    url = profile_data.get("url")
    token = profile_data.get("token")
    if not url or not token:
        raise ConfigError(f"{config_path}'s profile {profile_name!r} must set both url and token")
    return ResolvedConfig(url=url, token=token)


def resolve_config(
    url: str | None = None,
    token: str | None = None,
    profile: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> ResolvedConfig:
    """Resolve the URL and token to use, checking sources in priority order.

    Parameters
    ----------
    url
        An explicit URL. Wins over every other source, but must be paired
        with `token`.
    token
        An explicit token. Wins over every other source, but must be
        paired with `url`.
    profile
        An explicit profile name to select from the profile file, taking
        precedence over `LAKANA_PROFILE` and the file's own `default_profile`.
    environ
        The environment to read `LAKANA_URL`/`LAKANA_TOKEN`/`LAKANA_PROFILE`
        from. Defaults to `os.environ`.
    config_path
        Path to the profile file. Defaults to `default_config_path()`.

    Returns
    -------
    ResolvedConfig
        The resolved URL and token.

    Raises
    ------
    ConfigError
        If a source is only partially specified (a URL with no token, or
        vice versa; a profile missing a key), if the profile file is
        unsafe to read or malformed, or if no source resolves anything.
    """
    env = environ if environ is not None else os.environ

    if url is not None or token is not None:
        if url is None or token is None:
            raise ConfigError("Both url and token must be given together.")
        return ResolvedConfig(url=url, token=token)

    env_url = env.get("LAKANA_URL")
    env_token = env.get("LAKANA_TOKEN")
    if env_url is not None or env_token is not None:
        if env_url is None or env_token is None:
            raise ConfigError("Both LAKANA_URL and LAKANA_TOKEN must be set together.")
        return ResolvedConfig(url=env_url, token=env_token)

    path = config_path if config_path is not None else default_config_path(env)
    from_file = _resolve_from_file(path, profile, env)
    if from_file is not None:
        return from_file

    raise ConfigError(
        "No credentials found. Pass url/token explicitly, set LAKANA_URL and "
        f"LAKANA_TOKEN, or write a profile to a config.toml file (checked {path})."
    )
