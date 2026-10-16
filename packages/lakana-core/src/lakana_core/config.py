"""Configuration models for lakana-core."""

import logging
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from lakana_core.identifiers import shown
from lakana_core.log_setup import LOG_LEVELS


def _redact_database_url(url: str) -> str:
    """Render a database URL with any password hidden.

    Parameters
    ----------
    url
        A SQLAlchemy database URL, as stored in `DatabaseConfig.url`.

    Returns
    -------
    str
        `url` rendered through `sqlalchemy.engine.make_url` and
        `render_as_string(hide_password=True)`, or ``"***"`` if `url`
        cannot be parsed as a SQLAlchemy URL at all — an unparseable value
        might still carry a credential of its own, so it is redacted
        wholesale rather than echoed on a best-effort basis.
    """
    try:
        return make_url(url).render_as_string(hide_password=True)
    except ArgumentError:
        return "***"


class StorageConfig(BaseModel):
    """Where an environment's data lives, and how to reach it.

    Parameters
    ----------
    scheme
        Storage backend to use.
    root
        Root URI for the environment, such as ``file:///var/lakana/dev``.
    options
        Backend-specific options passed through to fsspec and pyiceberg,
        such as an endpoint or a region. `StorageResolver.for_environment`
        can merge a decrypted secret's values into this mapping, so its
        values never appear in this model's `repr`/`str` — see
        `__repr_args__`.
    """

    scheme: Literal["file", "s3", "abfs"]
    root: str
    options: dict[str, str] = {}

    def __repr_args__(self) -> Iterable[tuple[str | None, Any]]:
        """Yield repr fields with every `options` value redacted.

        `options` can hold a decrypted credential merged in by
        `StorageResolver.for_environment`
        (`lakana_core.storage.resolver._resolve_secret_values`). Every value
        is replaced with ``"***"`` unconditionally, keys included, rather
        than only values matching a secret-looking key name: `options` is
        backend configuration nothing needs to read back out of a `repr`,
        an endpoint or a region gains nothing from being printed either,
        and unconditional masking has no key-name heuristic (`secret`,
        `key`, `token`, ...) for a future backend's option name to fall
        outside of. `scheme` and `root` are structural, not secret, and
        stay visible.

        Yields
        ------
        tuple of (str or None, Any)
            Each field's name and value, as
            `pydantic.BaseModel.__repr_args__` defines them, with every
            `options` value replaced by ``"***"``.
        """
        for name, value in super().__repr_args__():
            if name == "options":
                yield name, dict.fromkeys(value, "***")
            else:
                yield name, value


class DatabaseConfig(BaseModel):
    """How to reach the metadata database.

    Parameters
    ----------
    url
        SQLAlchemy database URL. Its `repr`/`str` show the URL with any
        password redacted — see `__repr_args__`.
    echo
        Whether SQLAlchemy should log the SQL it emits.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    url: str
    echo: bool = False

    def __repr_args__(self) -> Iterable[tuple[str | None, Any]]:
        """Yield repr fields with `url`'s password redacted.

        Yields
        ------
        tuple of (str or None, Any)
            Each field's name and value, with `url` replaced by
            `_redact_database_url(url)`.
        """
        for name, value in super().__repr_args__():
            if name == "url":
                yield name, _redact_database_url(value)
            else:
                yield name, value


class SecretsConfig(BaseModel):
    """Configuration for the secrets subsystem.

    Parameters
    ----------
    master_key
        Base64-encoded 32-byte master key that wraps every secret's own
        data key (`lakana_core.secrets.keys.EnvKeyProvider`). `None` (the
        default) means no secrets can be created or decrypted yet — this
        does not fail startup, only the first actual secret operation,
        so a deployment that never uses secret-backed storage needs no
        configuration here at all. Its `repr`/`str` show ``"***"`` when
        set and `None` when unset — see `__repr_args__`.
    accept_format_1
        Whether to read secrets stored in the old format (1), written before
        the stored format bound a secret to its row. `False` (the default)
        refuses such a secret with a message that says how to set it again.
        `True` reads them, which accepts any old-format ciphertext written
        under the master key on any row; the server and the worker log a
        WARNING at start-up while it is on. Set it
        (`LAKANA_SECRETS__ACCEPT_FORMAT_1=true`) on the server and on every
        worker, and remove it once every secret is stored again.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    master_key: str | None = None
    accept_format_1: bool = False

    @field_validator("accept_format_1", mode="before")
    @classmethod
    def _accept_format_1_is_true_or_false(cls, value: object) -> bool:
        """Accept only ``true`` or ``false`` (any case), ``1`` or ``0``.

        Parameters
        ----------
        value
            The configured value: a bool, or the text of the environment variable.

        Returns
        -------
        bool
            The setting.

        Raises
        ------
        ValueError
            If it is anything else. The message never contains the value.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("true", "1"):
                return True
            if text in ("false", "0"):
                return False
        raise ValueError(
            "must be true or false, e.g. LAKANA_SECRETS__ACCEPT_FORMAT_1=true. It is false "
            "unless you set old-format secrets to be read (see the upgrade page)."
        )

    def __repr_args__(self) -> Iterable[tuple[str | None, Any]]:
        """Yield the `master_key` field redacted, then `accept_format_1`.

        Adding a field to this model means revisiting this method to yield
        it too.

        Yields
        ------
        tuple of (str or None, Any)
            ``("master_key", "***")`` when set, or ``("master_key", None)``
            when unset; then ``("accept_format_1", <bool>)``.
        """
        yield "master_key", ("***" if self.master_key is not None else None)
        yield "accept_format_1", self.accept_format_1


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s")
_USERINFO_PART = re.compile(r"(?:[A-Za-z0-9._~!$&'()*+,;=-]|%[0-9A-Fa-f]{2})+")
_URL_MESSAGE = "must be an http:// or https:// URL with a host, e.g. {example}"
_EMPTY_PORT_MESSAGE = (
    "the port after the colon is missing: write a port number, e.g. {example}, or drop the colon"
)


_TOKEN_LIKE_SEGMENT = re.compile(r"(?=[^/]*[0-9A-Z])[A-Za-z0-9._~%+-]{16,}")
"""A URL path segment that looks like a token: 16 or more URL-safe characters with a digit or a
capital letter in them. A long word of lowercase letters (a repository's name) does not match.
Real project and repository names match too (a cloud project id with digits), so a match is a
warning at start-up (`warn_about_tokens_in_url_paths`), never a refusal."""


def warn_about_tokens_in_url_paths(
    logger: logging.Logger, urls: Mapping[str, SecretStr | None]
) -> None:
    """Warn, once per setting, about a URL whose path has a segment that looks like a token.

    The log redactor removes a URL's userinfo, not its path, so a credential in the path would
    reach the logs. A project or repository name can look the same, so this warns and never
    refuses. The message names the setting and quotes neither the URL nor the segment. Called
    when the server and the worker start.

    Parameters
    ----------
    logger
        The process's logger.
    urls
        Each URL setting's environment variable name, and its value or `None`.
    """
    for name, value in urls.items():
        if value is None:
            continue
        path = urlsplit(value.get_secret_value()).path
        if any(_TOKEN_LIKE_SEGMENT.fullmatch(segment) for segment in path.split("/")):
            logger.warning(
                "%s has a path segment that looks like a token. A credential in a URL's path is "
                "not redacted from logs. If it is one, put it in the URL's userinfo instead "
                "(https://user:token@host/path), which is redacted. If it is not one (a project "
                "or repository name), nothing needs doing.",
                name,
            )


def _empty_port(netloc: str) -> bool:
    """Say whether a URL's host is followed by a colon and no port.

    Parameters
    ----------
    netloc
        The URL's network location, which `urlsplit` reads as having no port in this case.

    Returns
    -------
    bool
        `True` for ``host:``, ``user:pw@host:`` and ``[::1]:``.
    """
    return netloc.rpartition("@")[2].endswith(":")


def validate_proxy_url(
    value: str, noun: str = "proxy", example: str = "http://proxy.corp:3128"
) -> None:
    """Check a proxy (or package index) URL's shape without ever quoting it.

    Parameters
    ----------
    value
        The URL.
    noun
        What the URL is, for the error message ("proxy", "index").
    example
        A sample URL for the error message.

    Raises
    ------
    ValueError
        If it is not an `http(s)://host[:port]` URL without whitespace, a query string or a
        fragment; if the host is followed by a colon and no port (with a message of its own); or
        if it has userinfo that is not ``user:password`` with both
        parts non-empty and percent-encoded (a username alone would make git ask
        for the password, and an unencoded ``@`` splits the userinfo ambiguously).
        The message never contains the value.
    """
    message = _URL_MESSAGE.format(example=example)
    if _CONTROL.search(value) or _WHITESPACE.search(value):
        raise ValueError(message)
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        raise ValueError(message) from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(message)
    if _empty_port(parts.netloc):
        raise ValueError(_EMPTY_PORT_MESSAGE.format(example=example))
    if parts.fragment:
        raise ValueError(message)
    if "?" in value:
        raise ValueError(
            f"{noun} URL must not have a query string: put a credential in the URL's "
            "userinfo instead (https://user:token@host/path), which is redacted from logs"
        )
    if "@" in parts.netloc:
        userinfo = parts.netloc.rpartition("@")[0]
        username, _, password = userinfo.partition(":")
        if not (
            _USERINFO_PART.fullmatch(username)
            and _USERINFO_PART.fullmatch(password)
            and "@" not in userinfo
        ):
            raise ValueError(
                f"{noun} URL must include a password with its username, both percent-encoded "
                "(user:password@host)"
            )


def _check_no_proxy(value: str) -> None:
    """Refuse a proxy bypass list that names no host, or has control characters.

    Parameters
    ----------
    value
        The comma-separated list.

    Raises
    ------
    ValueError
        If it has no entry besides spaces and commas, or contains a control character.
    """
    if not any(entry.strip() for entry in value.split(",")) or _CONTROL.search(value):
        raise ValueError("must be a non-empty, single-line, comma-separated host list")


def _check_absolute_path(value: Path) -> None:
    """Require an absolute, single-line path.

    Parameters
    ----------
    value
        The path.

    Raises
    ------
    ValueError
        If the path is relative or contains a control character.
    """
    if not value.is_absolute() or _CONTROL.search(str(value)):
        raise ValueError("must be an absolute, single-line file path")


_SERVER_URL_MESSAGE = (
    "LAKANA_RUN__SERVER_URL must be an http:// or https:// URL of the Lakana server, "
    "without credentials, query or fragment, e.g. http://server:8000"
)
_SERVER_URL_EMPTY_PORT_MESSAGE = (
    "LAKANA_RUN__SERVER_URL has a colon after the host and no port: write the port, "
    "e.g. http://server:8000, or drop the colon"
)


def _checked_server_url(value: str) -> str:
    """Validate the server URL a Python task's harness talks to.

    Parameters
    ----------
    value
        The URL.

    Returns
    -------
    str
        The URL without a trailing slash.

    Raises
    ------
    ValueError
        If it is not an `http(s)://host[:port][/path]` URL, or carries
        userinfo, a query or a fragment, or if the host is followed by a colon and no port
        (with a message of its own). The message never contains the value.
    """
    if _CONTROL.search(value) or _WHITESPACE.search(value):
        raise ValueError(_SERVER_URL_MESSAGE)
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        raise ValueError(_SERVER_URL_MESSAGE) from None
    if parts.scheme in ("http", "https") and parts.hostname and _empty_port(parts.netloc):
        raise ValueError(_SERVER_URL_EMPTY_PORT_MESSAGE)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
    ):
        raise ValueError(_SERVER_URL_MESSAGE)
    return value.rstrip("/")


class RunConfig(BaseModel):
    """Operator settings of the run engine (`LAKANA_RUN__*`; Python assets too).

    Validation errors never echo a value: the proxy and index URLs may carry a password.

    Parameters
    ----------
    max_retries
        Most retries a project may configure for one asset; deploy
        validation refuses more.
    scheduler_interval_seconds
        How often the scheduler holder ticks.
    lease_seconds
        How long a claimed task's lease lasts without renewal.
    renew_seconds
        How often a worker renews its lease; must be below `lease_seconds`.
    drain_grace_seconds
        On SIGTERM, how long a running task may finish before it is released.
    server_url
        Base URL of the Lakana server as a Python task's subprocess reaches it.
        Unset, this worker cannot run Python assets.
    venv_cache
        Absolute directory of the venv cache; `None` uses the worker's cache directory.
    venv_keep
        How many venvs to keep, most recently used first.
    venv_build_timeout
        Seconds one `uv sync` may run.
    venv_max_bytes
        Largest on-disk size of one venv while it is built.
    kill_grace
        Seconds between SIGTERM and SIGKILL when a Python task is stopped.
    sweep_interval
        Seconds between the worker's sweeps of attempt directories a dead worker left
        behind (also at worker start), and between the sweeps of the artifact store that the
        worker holding the scheduler role runs (expired uploads, files and manifest objects
        no version names); 0 turns both off, and then nothing collects those leftovers.
    index_url
        An extra package index for the venv build (a `SecretStr`: it may carry a password).
    https_proxy
        Proxy of the venv build (a `SecretStr`).
    no_proxy
        Comma-separated hosts that bypass the proxy.
    ca_bundle
        Absolute path of a PEM file the venv build trusts; only its format is checked here.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    max_retries: int = Field(default=5, ge=0)
    scheduler_interval_seconds: float = Field(default=1.0, gt=0)
    lease_seconds: int = Field(default=60, gt=0)
    renew_seconds: int = Field(default=20, gt=0)
    drain_grace_seconds: float = Field(default=20.0, ge=0)
    server_url: str | None = None
    venv_cache: Path | None = None
    venv_keep: int = Field(default=10, ge=1)
    venv_build_timeout: float = Field(default=600.0, gt=0)
    venv_max_bytes: int = Field(default=5 * 1024**3, gt=0)
    kill_grace: float = Field(default=10.0, ge=0)
    sweep_interval: float = Field(default=3600.0, ge=0)
    index_url: SecretStr | None = None
    https_proxy: SecretStr | None = None
    no_proxy: str | None = None
    ca_bundle: Path | None = None

    @field_validator("server_url")
    @classmethod
    def _server_url_is_a_plain_http_url(cls, value: str | None) -> str | None:
        """Validate and normalise the server URL.

        Parameters
        ----------
        value
            The URL, or `None`.

        Returns
        -------
        str or None
            The URL without a trailing slash.
        """
        return None if value is None else _checked_server_url(value)

    @field_validator("index_url", "https_proxy")
    @classmethod
    def _urls_are_http_urls(cls, value: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        """Validate the index and proxy URLs with `validate_proxy_url`.

        Parameters
        ----------
        value
            The URL, or `None`.
        info
            Validation context; names the field for the error message.

        Returns
        -------
        SecretStr or None
            `value`, unchanged.
        """
        if value is not None:
            if info.field_name == "index_url":
                validate_proxy_url(
                    value.get_secret_value(), noun="index", example="https://pypi.corp/simple"
                )
            else:
                validate_proxy_url(value.get_secret_value())
        return value

    @field_validator("no_proxy")
    @classmethod
    def _no_proxy_is_one_line(cls, value: str | None) -> str | None:
        """Refuse an empty list or control characters.

        Parameters
        ----------
        value
            The bypass list, or `None`.

        Returns
        -------
        str or None
            `value`, unchanged.
        """
        if value is not None:
            _check_no_proxy(value)
        return value

    @field_validator("venv_cache", "ca_bundle")
    @classmethod
    def _paths_are_absolute(cls, value: Path | None) -> Path | None:
        """Require absolute, single-line paths.

        Parameters
        ----------
        value
            The path, or `None`.

        Returns
        -------
        Path or None
            `value`, unchanged.
        """
        if value is not None:
            _check_absolute_path(value)
        return value

    @model_validator(mode="after")
    def _renewal_beats_expiry(self) -> Self:
        """Require renewals to come before the lease lapses.

        Returns
        -------
        Self
            `self`.

        Raises
        ------
        ValueError
            If `renew_seconds` is not below `lease_seconds`.
        """
        if self.renew_seconds >= self.lease_seconds:
            raise ValueError("LAKANA_RUN__RENEW_SECONDS must be below LAKANA_RUN__LEASE_SECONDS")
        return self


class GitConfig(BaseModel):
    """Operator network settings for the worker's git fetches (`LAKANA_GIT__*`).

    The worker runs git with a minimal environment, so `HTTPS_PROXY`, `NO_PROXY`
    and a CA store variable of the worker are never seen by it; these settings
    are passed to git explicitly instead, for HTTPS repositories only. SSH is
    unaffected (`ProxyCommand` is not supported). Only the worker uses them: the
    server and `migrate` load but ignore them, so the CA file need not exist
    there (the worker checks it at startup). Validation errors never echo the
    offending value, which may carry a proxy password.

    Parameters
    ----------
    https_proxy
        ``http://`` or ``https://`` URL of the proxy git tunnels through
        (``http://user:password@host:3128``; the password is required with a
        username, and both are percent-encoded). A `SecretStr`: `repr`, `str` and
        `model_dump` hide it.
    no_proxy
        Comma-separated hosts, domains or CIDRs that bypass the proxy.
    ca_bundle
        Absolute path of a PEM file of the CAs git trusts for HTTPS (replaces the
        system store). Only its format is checked here.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    https_proxy: SecretStr | None = None
    no_proxy: str | None = None
    ca_bundle: Path | None = None

    @field_validator("https_proxy")
    @classmethod
    def _proxy_is_an_http_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Validate the proxy URL with `validate_proxy_url`.

        Parameters
        ----------
        value
            The proxy URL, or `None`.

        Returns
        -------
        SecretStr or None
            `value`, unchanged.
        """
        if value is not None:
            validate_proxy_url(value.get_secret_value())
        return value

    @field_validator("no_proxy")
    @classmethod
    def _no_proxy_is_one_line(cls, value: str | None) -> str | None:
        """Refuse an empty list or control characters.

        Parameters
        ----------
        value
            The bypass list, or `None`.

        Returns
        -------
        str or None
            `value`, unchanged.

        Raises
        ------
        ValueError
            If it is empty or contains a control character.
        """
        if value is not None:
            _check_no_proxy(value)
        return value

    @field_validator("ca_bundle")
    @classmethod
    def _ca_bundle_is_an_absolute_path(cls, value: Path | None) -> Path | None:
        """Require an absolute, single-line path (the file itself is checked by the worker).

        Parameters
        ----------
        value
            The path, or `None`.

        Returns
        -------
        Path or None
            `value`, unchanged.

        Raises
        ------
        ValueError
            If the path is relative or contains a control character.
        """
        if value is not None:
            _check_absolute_path(value)
        return value


_ALLOW_HOST = re.compile(
    r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)


class WebhookConfig(BaseModel):
    """Operator network settings for the worker's webhooks (`LAKANA_WEBHOOK__*`).

    The worker sends a webhook only to a public ``https`` address on port 443. These settings
    are the operator's way to widen or route that, and nothing else widens it: the worker's own
    ``HTTPS_PROXY`` and CA variables are never read. Only the worker uses them; the server and
    `migrate` load but ignore them, so the CA file need not exist there. Validation errors never
    echo the offending value, which may carry a proxy password.

    Parameters
    ----------
    allow_hosts
        Comma-separated host names, or ``*.domain`` for every subdomain of a domain (not the
        domain itself), that skip the address check and may use ``http`` and any port. Matching
        ignores case and a trailing dot. For an internal chat server.
    proxy
        ``http://`` or ``https://`` URL of the proxy webhooks tunnel through with ``CONNECT``
        (``http://user:password@host:3128``). The worker still resolves and checks the
        destination name, but the proxy resolves it again to connect, so the proxy is trusted
        to refuse internal addresses. A `SecretStr`: `repr`, `str` and `model_dump` hide it.
    ca_bundle
        Absolute path of a PEM file of the CAs webhooks trust (replaces the built-in store, never
        disables verification). Only its format is checked here.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    allow_hosts: str | None = None
    proxy: SecretStr | None = None
    ca_bundle: Path | None = None

    @field_validator("allow_hosts")
    @classmethod
    def _allow_hosts_are_names(cls, value: str | None) -> str | None:
        """Require a comma-separated list of host names or ``*.domain`` patterns.

        Parameters
        ----------
        value
            The list, or `None`.

        Returns
        -------
        str or None
            `value`, unchanged.

        Raises
        ------
        ValueError
            If an entry is empty or is not a host name; the message does not quote the value.
        """
        if value is not None:
            entries = [entry.strip().rstrip(".") for entry in value.split(",")]
            # A number in the last label would let `*.0.0.1` match an IP address.
            if not all(
                _ALLOW_HOST.match(entry) and not entry.rsplit(".", 1)[-1].isdigit()
                for entry in entries
            ):
                raise ValueError(
                    "LAKANA_WEBHOOK__ALLOW_HOSTS must be a comma-separated list of host names "
                    "or *.domain patterns, such as chat.corp.example,*.hooks.example; "
                    "an IP address cannot be allowed, use its host name"
                )
        return value

    def allowed_hosts(self) -> tuple[str, ...]:
        """Return the allow-list, lower-cased and without trailing dots.

        Returns
        -------
        tuple[str, ...]
            The host names and ``*.domain`` patterns; empty when none are set.
        """
        if self.allow_hosts is None:
            return ()
        return tuple(entry.strip().rstrip(".").lower() for entry in self.allow_hosts.split(","))

    @field_validator("proxy")
    @classmethod
    def _proxy_is_an_http_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Validate the proxy URL with `validate_proxy_url`.

        Parameters
        ----------
        value
            The proxy URL, or `None`.

        Returns
        -------
        SecretStr or None
            `value`, unchanged.
        """
        if value is not None:
            validate_proxy_url(value.get_secret_value())
        return value

    @field_validator("ca_bundle")
    @classmethod
    def _ca_bundle_is_an_absolute_path(cls, value: Path | None) -> Path | None:
        """Require an absolute, single-line path (the file itself is checked by the worker).

        Parameters
        ----------
        value
            The path, or `None`.

        Returns
        -------
        Path or None
            `value`, unchanged.
        """
        if value is not None:
            _check_absolute_path(value)
        return value


class DenialAuditConfig(BaseModel):
    """Operator-level bound on `AUTHORIZATION_DENIED` audit rows.

    Parameters
    ----------
    per_principal_per_minute
        Denials recorded per principal per minute, by each server process and each worker
        (`lakana_core.audit.denial_limiter.DenialAuditLimiter`). A denial past it is still
        refused, but not recorded; the next row recorded for that principal carries the number
        not recorded as `detail.suppressed`, and each one is written to the process log at INFO.
        The first denial of each action by a principal is always recorded.
    """

    per_principal_per_minute: int = Field(default=30, gt=0)


DEFAULT_MAX_STATEMENT_CHARS = 100_000
"""The default of `QueryLimitsConfig.max_statement_chars` and of a deploy's SQL limit."""


_CAP_DIGITS = 9
"""A cap written with more digits is read as 999999999, which is no limit in practice."""


class QueryLimitsConfig(BaseModel):
    """Operator-level bounds on one query's execution and result.

    Parameters
    ----------
    max_result_rows
        Most rows a query may return. Exceeding it fails the query; the
        result is never truncated.
    max_result_bytes
        Most bytes a result may occupy as in-memory Arrow buffers — a
        memory-exhaustion guard, not an on-disk or wire size. Checked
        between fetched batches (`lakana_worker.executor._FETCH_BATCH_ROWS`),
        so a result can overshoot this by up to one batch's worth of the
        query's largest value, not by an unbounded amount.
    memory_limit
        DuckDB `memory_limit` for the query's connection, in DuckDB's own
        syntax (``"1GB"``, ``"512MiB"``). Bounds execution, not just the
        result.
    max_write_rows
        Most rows a write run may write. Exceeding it fails the run
        and deletes every file it wrote. Writes are deliberately not bound
        by `max_result_bytes`: they stream to storage, never into a result.
    timeout_seconds
        Wall-clock limit for one query, in seconds. For a write run it covers the whole
        streaming phase, not just the start of the query, so time spent encoding and
        uploading to storage counts: a slow store can fail a write with the time-limit
        message while DuckDB is idle. Hard limit: the query runs in a process of its
        own; at the limit the worker asks it to stop and, `kill_grace_seconds` later, kills it.
    kill_grace_seconds
        Seconds between asking a query to stop (its time limit, a cancel, the worker's
        drain) and killing its process. 0 kills at once; it must be a finite number. A query that
        stops when asked ends with DuckDB's own clean error; one that had to be killed after its
        time limit or a cancel request says so in its message (a task's cancel and the worker's
        drain only log it).
    threads
        DuckDB `threads` for the query's connection, so one query cannot
        take every CPU core.
    max_temp_directory_size
        DuckDB `max_temp_directory_size` for the per-run spill directory, in
        DuckDB's own syntax (``"10GB"``). Spilling only happens where the
        local filesystem is enabled (local `file://` mode): every other mode
        disables it for user SQL, which also blocks DuckDB's spill files, so a
        query over `memory_limit` fails there instead of spilling.
    max_statement_chars
        Longest SQL text, in characters, accepted at submit: a query, a write, a row-filter or
        column-mask expression, and a SQL asset at deploy. The SQL parser's time and
        memory grow with the text and are outside the query timeout (30 MB of nested
        subqueries took 19 s and 2.9 GB); the default is far above any statement written by
        hand.
    running_per_principal
        The most ad-hoc queries one user or service account runs at once, over every
        environment: `auto` or a whole number of 1 or more. A further query waits
        and is not refused. With `auto`, one principal may run ad-hoc queries on every worker
        that takes interactive work while there are one or two such workers, and on all of
        them but one from three on. Pipeline work is never counted. The server reads it too,
        for the reason a query waits.

    Notes
    -----
    The time limit is hard. `memory_limit` is DuckDB's own target inside the query's process and
    can be overshot; on Linux the worker's memory limit (`WorkerConfig.memory_limit`) then stops
    the largest query first, and the worker only if that was not enough. On macOS
    the worker stops without ending a query first.
    """

    max_result_rows: int = Field(default=1_000_000, gt=0)
    max_result_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    memory_limit: str = "1GB"
    max_write_rows: int = Field(default=100_000_000, gt=0)
    timeout_seconds: float = Field(default=900, gt=0)
    kill_grace_seconds: float = Field(default=10, ge=0, allow_inf_nan=False)
    threads: int = Field(default=4, gt=0)
    max_temp_directory_size: str = "10GB"
    max_statement_chars: int = Field(default=DEFAULT_MAX_STATEMENT_CHARS, gt=0)
    running_per_principal: int | Literal["auto"] = "auto"

    @field_validator("running_per_principal", mode="before")
    @classmethod
    def _auto_or_a_whole_number(cls, value: object) -> int | Literal["auto"]:
        """Accept `auto` in any letter case, or a whole number of 1 or more.

        Parameters
        ----------
        value
            The configured value: an int, or the text of the environment variable.

        Returns
        -------
        int or Literal["auto"]
            The setting.

        Raises
        ------
        ValueError
            If it is anything else (a bool, 0, a negative or fractional number, other text).
        """
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text == "auto":
                return "auto"
            if text.isascii() and text.isdigit():
                number = int(text) if len(text) <= _CAP_DIGITS else 10**_CAP_DIGITS - 1
                if number >= 1:
                    return number
        raise ValueError(
            "LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL must be auto or a whole number of 1 or "
            f"more (got '{shown(str(value))}'). auto reserves one worker for other users once "
            "three or more workers take interactive work."
        )


def validate_repo_roots(roots: list[str]) -> list[str]:
    """Require every `LAKANA_REPO_ROOTS` entry to be an absolute path.

    Shared by the worker's and the server's settings.

    Parameters
    ----------
    roots
        The configured roots.

    Returns
    -------
    list of str
        `roots`, unchanged.

    Raises
    ------
    ValueError
        If an entry is not absolute (pydantic reports it as a settings error).
    """
    for root in roots:
        if not os.path.isabs(root):
            raise ValueError(f"LAKANA_REPO_ROOTS entries must be absolute paths, got {root!r}.")
    return roots


_MEMORY_UNITS = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}
_MEMORY_SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([a-z]*)", re.ASCII)
_MEMORY_TEXT_MAX = 32
_MEMORY_MAX = 1024**5
MIN_WORKER_MEMORY = 512 * 1024**2
"""Least a worker may be limited to: an idle worker is 170 to 190 MB resident, and the first
queries add to that, so 512 MB leaves about a factor of 2.5."""
DEFAULT_WORKER_MEMORY = "4GB"
"""The worker's limit when nothing else says."""


def parse_memory_limit(text: str) -> int | None:
    """Parse `LAKANA_WORKER__MEMORY_LIMIT`.

    Parameters
    ----------
    text
        A size such as ``"4GB"``, ``"4g"``, ``"512MB"`` or ``"3600m"``: ASCII digits and a
        unit (``B``, ``K``, ``M``, ``G``, ``T`` with an optional ``B`` or ``iB``, any case),
        or ``"0"``, ``"0b"`` or ``"off"`` for no limit. Units are binary here, as in Docker:
        ``GB`` is 1024 MB, and ``GiB`` and ``MiB`` mean the same as ``GB`` and ``MB``.
        (`LAKANA_QUERY_LIMITS__MEMORY_LIMIT` is read by DuckDB, where ``GB`` is decimal
        and ``GiB`` binary: ``1GB`` is 953.6 MiB.)

    Returns
    -------
    int or None
        The limit in bytes, or `None` for no limit.

    Raises
    ------
    ValueError
        If `text` is none of those, is more than 1 PiB, or is less than 512 MB. The
        message names the setting.
    """
    cleaned = text.strip().lower()
    if cleaned in {"0", "0b", "off"}:
        return None
    syntax = ValueError(
        "LAKANA_WORKER__MEMORY_LIMIT must be a size with a unit, such as 4GB, 512MB or 4g "
        "(binary: 1GB is 1024MB), or off for no limit."
    )
    matched = _MEMORY_SIZE.fullmatch(cleaned) if len(cleaned) <= _MEMORY_TEXT_MAX else None
    if matched is None or matched.group(2) not in _MEMORY_UNITS:
        raise syntax
    size = int(float(matched.group(1)) * _MEMORY_UNITS[matched.group(2)])
    if size == 0 or size > _MEMORY_MAX:
        raise syntax
    if size < MIN_WORKER_MEMORY:
        raise ValueError(
            "LAKANA_WORKER__MEMORY_LIMIT must be at least 512MB: a worker needs about that much "
            "to start. Use off to run without a limit."
        )
    return size


WORKER_TAKES: tuple[str, ...] = ("all", "interactive", "pipeline")
"""What `LAKANA_WORKER__TAKES` may be: all work, ad-hoc queries only, or pipeline work only."""


class WorkerConfig(BaseModel):
    """Settings of the worker process itself.

    Parameters
    ----------
    memory_limit
        The limit on the worker's resident memory, counting the Python tasks and `uv` it
        started (``"4GB"``, at least ``512MB``; ``"off"`` or ``"0"`` for none; see
        `parse_memory_limit`). Units are binary: ``GB`` and ``g`` mean 1024 MB, unlike
        DuckDB's `LAKANA_QUERY_LIMITS__MEMORY_LIMIT`, where ``GB`` is decimal and ``GiB``
        binary. A worker over the limit records why against what it was running, kills its
        tasks and exits; Docker or the local-mode supervisor restarts it. When unset, the
        worker uses 90 percent of its container's memory limit, or 4GB without one. Linux
        measures the worker and its descendants; macOS only the worker process; Windows
        nothing.
    notifications
        Whether this worker delivers job notifications (`LAKANA_WORKER__NOTIFICATIONS`, on by
        default). Set to ``false`` on a worker that must not reach the webhook hosts (a worker
        in a network without outbound access); another worker delivers them. With no worker
        delivering, queued notifications wait.
    takes
        The work this worker takes (`LAKANA_WORKER__TAKES`): ``"all"`` (the default),
        ``"interactive"`` (ad-hoc queries) or ``"pipeline"`` (deploys, pipeline tasks and
        table maintenance), in any letter case. The worker says it when it registers.
    """

    memory_limit: str | None = None
    notifications: bool = True
    takes: str = "all"

    @field_validator("memory_limit")
    @classmethod
    def _memory_limit_parses(cls, value: str | None) -> str | None:
        """Refuse a value `parse_memory_limit` cannot read.

        Parameters
        ----------
        value
            The configured text, if any.

        Returns
        -------
        str or None
            `value`, unchanged.
        """
        if value is not None:
            parse_memory_limit(value)
        return value

    @field_validator("takes")
    @classmethod
    def _takes_is_one_of_three(cls, value: str) -> str:
        """Refuse a `takes` the worker does not know, and say what to write.

        Parameters
        ----------
        value
            The configured text.

        Returns
        -------
        str
            `value` in lower case, without surrounding blanks.

        Raises
        ------
        ValueError
            If it is not one of `WORKER_TAKES`.
        """
        cleaned = value.strip().lower()
        if cleaned not in WORKER_TAKES:
            raise ValueError(
                f"LAKANA_WORKER__TAKES must be all, interactive or pipeline (got '{shown(value)}')."
            )
        return cleaned


class LogConfig(BaseModel):
    """How much the server and the worker log (`LAKANA_LOG__LEVEL`).

    Parameters
    ----------
    level
        ``DEBUG``, ``INFO``, ``WARNING`` or ``ERROR``, in any letter case (kept in upper case).
        Lakana's own log lines, and uvicorn's, are printed from this level up; a library's only
        from WARNING. Above INFO the denials over the audit rate leave no line in the log (only
        the summary row in the audit table); uvicorn's access log, one line per request, is
        printed at INFO and DEBUG only.
    """

    level: str = "INFO"

    @field_validator("level")
    @classmethod
    def _level_is_one_of_four(cls, value: str) -> str:
        """Refuse a level the log set-up does not know, and say what to write.

        Parameters
        ----------
        value
            The configured text.

        Returns
        -------
        str
            `value` in upper case.

        Raises
        ------
        ValueError
            If it is not one of `lakana_core.log_setup.LOG_LEVELS`.
        """
        if value.upper() not in LOG_LEVELS:
            raise ValueError(
                f"LAKANA_LOG__LEVEL is '{shown(value)}'; set it to one of "
                f"{', '.join(LOG_LEVELS[:-1])} or {LOG_LEVELS[-1]} (any letter case). "
                "The default is INFO."
            )
        return value.upper()


class TaskTokensConfig(BaseModel):
    """Operator-level bounds on task-token minting.

    Parameters
    ----------
    max_ttl_seconds
        Longest `ttl_seconds` a minted task token may request. Enforced by
        `lakana_core.identity.task_tokens.TaskTokenService.mint`, so an
        unattended task's credential cannot outlive human review
        indefinitely by requesting an arbitrarily long lifetime.
    """

    max_ttl_seconds: int = Field(default=86_400, gt=0)


class ArtifactsConfig(BaseModel):
    """Operator-level bounds on the artifact store.

    Read from ``LAKANA_ARTIFACTS__MAX_FILE_BYTES``, ``LAKANA_ARTIFACTS__MAX_BYTES``,
    ``LAKANA_ARTIFACTS__MAX_FILES`` and ``LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS``.

    Parameters
    ----------
    max_file_bytes
        Largest single file of an artifact version (default 2 GiB).
    max_bytes
        Largest total size of one artifact version (default 10 GiB).
    max_files
        Most files in one artifact version (default 10,000).
    upload_ttl_seconds
        How long an open upload may stay uncommitted, and how long a commit of it can be repeated
        (default one hour).
    """

    max_file_bytes: int = Field(default=2 * 1024**3, gt=0)
    max_bytes: int = Field(default=10 * 1024**3, gt=0)
    max_files: int = Field(default=10_000, gt=0)
    upload_ttl_seconds: int = Field(default=3600, gt=0)


class Settings(BaseSettings):
    """Runtime settings for lakana-core.

    Parameters
    ----------
    database
        Metadata database configuration.
    bootstrap_admin
        Name of the principal created on first start and used until M3
        introduces real authentication.
    secrets
        Secrets subsystem configuration.
    query_limits
        Operator-level bounds on query execution and results
        (`LAKANA_QUERY_LIMITS__*`), enforced by `lakana_worker.executor`.
    run
        Run engine settings (`LAKANA_RUN__*`).
    git
        Proxy and CA settings of the worker's git fetches (`LAKANA_GIT__*`).
    webhook
        Allow-list, proxy and CA settings of the worker's webhooks (`LAKANA_WEBHOOK__*`).
    worker
        The worker process's own settings (`LAKANA_WORKER__*`), such as its hard
        memory limit.
    log
        How much the server and the worker log (`LAKANA_LOG__LEVEL`, default INFO).
    task_tokens
        Bounds on task-token minting (`LAKANA_TASK_TOKENS__*`); the worker reads
        the TTL cap for the tokens it mints for Python tasks.
    denial_audit
        Bound on `AUTHORIZATION_DENIED` rows per principal (`LAKANA_DENIAL_AUDIT__*`).
    artifacts
        Artifact-store bounds (`LAKANA_ARTIFACTS__*`): file size, total size, file
        count and upload lifetime.
    repo_roots
        Absolute directories local repositories must lie under
        (`LAKANA_REPO_ROOTS`, a JSON list). Empty refuses every local
        repository.
    """

    model_config = SettingsConfigDict(
        env_prefix="LAKANA_", env_nested_delimiter="__", hide_input_in_errors=True
    )

    database: DatabaseConfig
    bootstrap_admin: str = "admin"
    secrets: SecretsConfig = SecretsConfig()
    query_limits: QueryLimitsConfig = QueryLimitsConfig()
    run: RunConfig = RunConfig()
    git: GitConfig = GitConfig()
    webhook: WebhookConfig = WebhookConfig()
    worker: WorkerConfig = WorkerConfig()
    log: LogConfig = LogConfig()
    task_tokens: TaskTokensConfig = TaskTokensConfig()
    denial_audit: DenialAuditConfig = DenialAuditConfig()
    artifacts: ArtifactsConfig = ArtifactsConfig()
    repo_roots: list[str] = []

    @field_validator("repo_roots")
    @classmethod
    def _repo_roots_are_absolute(cls, value: list[str]) -> list[str]:
        """Validate `LAKANA_REPO_ROOTS`.

        Parameters
        ----------
        value
            The parsed list.

        Returns
        -------
        list of str
            The list, unchanged.
        """
        return validate_repo_roots(value)
