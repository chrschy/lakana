"""Reading and writing the profile file the SDK reads (`config.toml`).

This module is the only writer of the file. Reading goes through the SDK's
own `_read_profile_file`, so the CLI enforces exactly the SDK's rules
(no symlink, no group/other permissions).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lakana._config import _read_profile_file, validate_profile_document
from lakana.cli._filelock import LockTimeout, locked
from lakana.errors import ConfigError

_LOCK_TIMEOUT = 10.0


def load(path: Path) -> dict[str, Any]:
    """Load the profile file, or return an empty document if it doesn't exist.

    Parameters
    ----------
    path
        The profile file.

    Returns
    -------
    dict
        The parsed TOML document.

    Raises
    ------
    ConfigError
        If the file is a symlink, too permissive, not valid TOML, or has the
        wrong shape (`lakana._config.validate_profile_document`).
    """
    text = _read_profile_file(path)
    if text is None:
        return {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    validate_profile_document(data, path)
    return data


def active_profile_name(data: dict[str, Any], profile: str | None) -> str | None:
    """Name the profile in effect: explicit, then `LAKANA_PROFILE`, then `default_profile`.

    Parameters
    ----------
    data
        The loaded profile file.
    profile
        An explicitly selected profile, if any.

    Returns
    -------
    str or None
        The profile name, or `None` if nothing selects one.
    """
    return profile or os.environ.get("LAKANA_PROFILE") or data.get("default_profile")


def default_environment(path: Path, profile: str | None) -> str | None:
    """Return the active profile's `default_environment`, if it has one.

    Parameters
    ----------
    path
        The profile file.
    profile
        An explicitly selected profile, if any.

    Returns
    -------
    str or None
        The environment name, or `None`.
    """
    data = load(path)
    name = active_profile_name(data, profile)
    if name is None:
        return None
    return data.get("profiles", {}).get(name, {}).get("default_environment")


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class ProfileSummary:
    """One profile as `lakana profile list` shows it — never with its token.

    Parameters
    ----------
    name
        The profile name.
    url
        Its platform URL.
    default_environment
        Its default environment, if any.
    is_default
        Whether it is the file's `default_profile`.
    """

    name: str
    url: str
    default_environment: str | None
    is_default: bool


def _quote(value: str) -> str:
    """Quote a string as a TOML basic string.

    JSON string escaping is a subset of TOML's basic-string escaping, with
    one gap: JSON does not require DEL (`0x7F`) to be escaped, but TOML basic
    strings forbid it unescaped, so it is escaped separately here.

    Parameters
    ----------
    value
        The string.

    Returns
    -------
    str
        The quoted form.
    """
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _key(name: str) -> str:
    """Render a TOML key, quoting it unless it is a bare key.

    Parameters
    ----------
    name
        The key.

    Returns
    -------
    str
        The key as it appears in the file.
    """
    return name if _BARE_KEY.fullmatch(name) else _quote(name)


def _string(value: object, where: str) -> str:
    """Require a string value, refusing anything the flat format can't hold.

    Parameters
    ----------
    value
        The value.
    where
        Its dotted location, for the error message.

    Returns
    -------
    str
        The value.

    Raises
    ------
    ConfigError
        If the value is not a string.
    """
    if not isinstance(value, str):
        raise ConfigError(
            f"Can't rewrite the profile file: {where} is not a string; edit it by hand."
        )
    return value


def dumps(data: dict[str, Any]) -> str:
    """Serialize the flat profile document as TOML.

    Parameters
    ----------
    data
        `{"default_profile"?: str, <other str keys>, "profiles": {name: {str: str}}}`.

    Returns
    -------
    str
        The TOML text.

    Raises
    ------
    ConfigError
        If any value is not a string (or `profiles` is not a table of tables).
    """
    lines: list[str] = []
    for key, value in data.items():
        if key == "profiles":
            continue
        lines.append(f"{_key(key)} = {_quote(_string(value, key))}")
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("Can't rewrite the profile file: profiles is not a table.")
    for name, entries in profiles.items():
        if not isinstance(entries, dict):
            raise ConfigError(f"Can't rewrite the profile file: profiles.{name} is not a table.")
        lines.append("")
        lines.append(f"[profiles.{_key(name)}]")
        for key, value in entries.items():
            lines.append(f"{_key(key)} = {_quote(_string(value, f'profiles.{name}.{key}'))}")
    return "\n".join(lines) + "\n"


def save(path: Path, data: dict[str, Any]) -> None:
    """Write the profile file atomically with mode 0600 in a 0700 directory.

    The text is written to a fresh temp file in the same directory (opened
    `O_CREAT | O_EXCL | O_NOFOLLOW`, mode 0600), fsynced, and then moved over
    the target with `os.replace`; on POSIX the directory is then fsynced too,
    so the rename itself survives a crash. The directory is created at mode
    0700 if it doesn't exist; if it already does, it is checked (never
    re-permissioned) the same way the SDK checks the file itself.

    Parameters
    ----------
    path
        The profile file.
    data
        The document to write.

    Raises
    ------
    ConfigError
        If `dumps` refuses the document, or if the parent directory is a
        symlink or is readable/writable by other users (POSIX only; skipped
        on Windows, where neither check applies).
    """
    text = dumps(data)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        parent_mode = os.lstat(path.parent).st_mode
        if stat.S_ISLNK(parent_mode):
            raise ConfigError(f"{path.parent} is a symlink; refusing to write into it")
        if stat.S_IMODE(parent_mode) & 0o077:
            raise ConfigError(
                f"{path.parent} is accessible by other users; run: chmod 700 {path.parent}"
            )
    temp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        # The rename lives in the directory entry; fsync the directory so it
        # survives a crash, not just the file's contents.
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


@contextmanager
def _update_lock(path: Path) -> Generator[None, None, None]:
    """Hold the profile file's update lock for a whole load, modify, save sequence.

    The lock is an exclusive advisory lock on `<path>.lock`, a sibling file
    (0600, never a symlink), so two `lakana login` runs sharing a home
    directory can't both load the same document and have the second save
    drop the first's change. Reads stay lock-free: `save` replaces
    the file atomically, so a reader sees either the old or the new document.

    Parameters
    ----------
    path
        The profile file.

    Yields
    ------
    None
        While the lock is held.

    Raises
    ------
    CliError
        If another process holds the lock for longer than `_LOCK_TIMEOUT`
        seconds (exit 3), or the lock file is unusable (see
        `lakana.cli._filelock`).
    """
    from lakana.cli._state import CliError

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with locked(path.with_name(path.name + ".lock"), timeout=_LOCK_TIMEOUT):
            yield
    except LockTimeout as exc:
        raise CliError(
            f"Another lakana process is updating {path} (waited {_LOCK_TIMEOUT:g} s); try again",
            3,
        ) from exc


def set_profile(
    path: Path, name: str, *, url: str, token: str, default_environment: str | None
) -> None:
    """Create or update one profile, keeping everything else in the file.

    Parameters
    ----------
    path
        The profile file.
    name
        The profile to write.
    url
        Its platform URL.
    token
        Its credential.
    default_environment
        Its default environment; `None` keeps whatever it had.
    """
    with _update_lock(path):
        data = load(path)
        profiles = data.setdefault("profiles", {})
        entry = dict(profiles.get(name, {}))
        entry["url"] = url
        entry["token"] = token
        if default_environment is not None:
            entry["default_environment"] = default_environment
        profiles[name] = entry
        if not data.get("default_profile"):
            rest = {k: v for k, v in data.items() if k != "default_profile"}
            data = {"default_profile": name, **rest}
        save(path, data)


def remove_profile(path: Path, name: str) -> None:
    """Delete one profile, clearing `default_profile` if it named it.

    Parameters
    ----------
    path
        The profile file.
    name
        The profile to delete.

    Raises
    ------
    CliError
        If there is no such profile (exit 2).
    """
    from lakana.cli._state import CliError

    with _update_lock(path):
        data = load(path)
        profiles = data.get("profiles", {})
        if name not in profiles:
            raise CliError(f"No profile named {name!r}")
        del profiles[name]
        if data.get("default_profile") == name:
            del data["default_profile"]
        if not profiles:
            data.pop("profiles")
        save(path, data)


def use_profile(path: Path, name: str, environment: str | None) -> None:
    """Make a profile the default, optionally setting its default environment.

    Parameters
    ----------
    path
        The profile file.
    name
        The profile.
    environment
        A new `default_environment`, or `None` to leave it.

    Raises
    ------
    CliError
        If there is no such profile (exit 2).
    """
    from lakana.cli._state import CliError

    with _update_lock(path):
        data = load(path)
        profiles = data.get("profiles", {})
        if name not in profiles:
            raise CliError(f"No profile named {name!r}")
        if environment is not None:
            profiles[name]["default_environment"] = environment
        data["default_profile"] = name
        save(path, data)


def list_profiles(path: Path) -> list[ProfileSummary]:
    """Summarize every profile, without tokens.

    Parameters
    ----------
    path
        The profile file.

    Returns
    -------
    list of ProfileSummary
        In file order.
    """
    data = load(path)
    default = data.get("default_profile")
    return [
        ProfileSummary(
            name=name,
            url=entries.get("url", ""),
            default_environment=entries.get("default_environment"),
            is_default=name == default,
        )
        for name, entries in data.get("profiles", {}).items()
    ]
