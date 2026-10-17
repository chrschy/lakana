"""Which package sources a project's lock may name.

Pure: no database, no web framework. The registry of indexes
(`lakana_core.projects.package_indexes`) stores what `normalize_index_url`
returns, and `lakana_core.projects.lockfile` compares a lock's sources with
`KnownSources`.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit, urlunsplit

from lakana_core.errors import ValidationError

PYPI_URL = "https://pypi.org/simple"
"""The public index; a lock may always name it."""

WHEELHOUSE = "wheelhouse"
"""The one folder of a project a lock may name as a flat index."""

_MAX_URL = 1024
_PUBLIC_HOSTS = frozenset(
    {
        "pypi.org",
        "www.pypi.org",
        "upload.pypi.org",
        "pypi.python.org",
        "files.pythonhosted.org",
        "test-files.pythonhosted.org",
    }
)
"""The public index's hosts that are never registered: PyPI itself is a known source without
registration, and the others are not indexes."""
_TESTPYPI_HOST = "test.pypi.org"
"""TestPyPI: an index of its own, which may be registered, but never with a credential.
Together with `_PUBLIC_HOSTS` these are the seven hosts no credential may go to."""
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_HOST = 253
_MAX_LABEL = 63
_PRINTABLE_ASCII = re.compile(r"[\x21-\x7e]*")
_DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
_HEX_LABEL = re.compile(r"0x[0-9a-f]*")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~+@-]+")
_EXAMPLE = "for example https://pkgs.example.org/simple"


def _is_loopback(host: str) -> bool:
    """Say whether a host name is this machine.

    Parameters
    ----------
    host
        A host name or an IP literal, lower case, without brackets.

    Returns
    -------
    bool
        `True` for ``localhost`` and for a loopback IPv4 or IPv6 address.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Refused(Exception):
    """A URL breaks a rule; the text is the message a person registering it reads."""


def _canonical_host(parts: SplitResult) -> str:
    """Check the host and return its one stored spelling.

    Parameters
    ----------
    parts
        The split URL, already free of userinfo.

    Returns
    -------
    str
        An ASCII DNS name in lower case, or an IP literal exactly as
        `ipaddress` prints it (an IPv6 one in brackets).

    Raises
    ------
    _Refused
        If the host is not one of those, so that Python and uv could read it as
        two different hosts or as two spellings of one.
    """
    host = parts.hostname or ""
    if parts.netloc.startswith("["):
        try:
            canonical = str(ipaddress.IPv6Address(host))
        except ValueError:
            canonical = ""
        if "%" in host:
            raise _Refused("The package index URL must not name an IPv6 zone (no '%' in the host).")
        if canonical and ipaddress.IPv6Address(host).ipv4_mapped is not None:
            raise _Refused(
                "The package index URL must write an IPv4-mapped address as the IPv4 address "
                "itself: write the IPv4 address, for example 192.0.2.10."
            )
        if canonical != host:
            raise _Refused(
                "The package index URL must give an IPv6 address in its shortest form, "
                "for example [::1] or [2001:db8::1]."
            )
        return f"[{host}]"
    labels = host.split(".")
    if host.endswith("."):
        raise _Refused(
            "The package index URL host must not have a trailing dot (write pkgs.example.org)."
        )
    if not host or "" in labels:
        raise _Refused(
            "The package index URL host must not have an empty label (two dots in a row)."
        )
    if len(host) > _MAX_HOST or any(len(label) > _MAX_LABEL for label in labels):
        raise _Refused(
            f"The package index URL host labels may have at most {_MAX_LABEL} characters "
            f"and the host at most {_MAX_HOST}."
        )
    if _NUMERIC_LABEL.fullmatch(labels[-1]) or any(_HEX_LABEL.fullmatch(x) for x in labels):
        try:
            canonical = str(ipaddress.IPv4Address(host))
        except ValueError:
            canonical = ""
        if canonical != host:
            raise _Refused(
                "The package index URL host looks like an IPv4 address that is not written as four "
                "decimal numbers; write it as 192.0.2.10, or use the host name."
            )
        return host
    if not all(_DNS_LABEL.fullmatch(label) for label in labels):
        raise _Refused(
            "The package index URL host may contain only letters, digits and hyphens between "
            "dots (no '%'); write an international name in its xn-- form."
        )
    return host


def _canonical_path(path: str) -> str:
    """Check the path and return it without trailing slashes.

    Parameters
    ----------
    path
        The URL's path.

    Returns
    -------
    str
        The path, which has no empty segment and no dot segment.

    Raises
    ------
    _Refused
        If a segment is empty, is ``.`` or ``..``, holds ``%``, or holds a
        character other than letters, digits, ``-``, ``.``, ``_``, ``~``, ``+``
        and ``@``.
    """
    path = path.rstrip("/")
    for segment in path[1:].split("/") if path else []:
        if "%" in segment:
            raise _Refused(
                "A package index URL path with a percent sign or a space is not supported for a "
                "registered index; use a URL whose path has none (for example a numeric project "
                "ID instead of a name)."
            )
        if segment == "":
            raise _Refused("The package index URL path must not have an empty segment ('//').")
        if segment in (".", ".."):
            raise _Refused("The package index URL path must not contain a '.' or '..' segment.")
        if not _PATH_SEGMENT.fullmatch(segment):
            raise _Refused(
                "The package index URL path may contain only letters, digits and - . _ ~ + @ "
                "between slashes."
            )
    return path


def _canonical(url: str) -> str:
    """Apply every rule to an index URL and return its one stored spelling.

    Parameters
    ----------
    url
        A URL from a registration, a lock, a `pyproject.toml` or a setting.

    Returns
    -------
    str
        Scheme, host, a port that is not the scheme's default, and path, in the
        one form Python and uv read as the same origin.

    Raises
    ------
    _Refused
        On the first rule broken. No message contains any part of the URL: a
        rejected one may carry a credential.
    """
    if not isinstance(url, str):
        raise _Refused(f"The package index URL must be text, {_EXAMPLE}.")
    if len(url) > _MAX_URL:
        raise _Refused(f"The package index URL is longer than {_MAX_URL} characters.")
    if not _PRINTABLE_ASCII.fullmatch(url) or "\\" in url:
        raise _Refused(
            "The package index URL must not contain whitespace, control characters, a backslash "
            "or non-ASCII characters; write an international host name in its xn-- form."
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise _Refused(
            f"The package index URL is malformed; give https://host/path, {_EXAMPLE}."
        ) from exc
    if "@" in parts.netloc:
        raise _Refused(
            "The package index URL must not contain a user name or a password. Store them in a "
            "secret and name it with --credential-secret."
        )
    if "?" in url or "#" in url:
        raise _Refused("The package index URL must not contain '?' or '#'.")
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        raise _Refused(
            f"The package index URL must start with https:// and name a host, {_EXAMPLE}."
        )
    if port == 0:
        raise _Refused("The package index URL port must be between 1 and 65535.")
    host = _canonical_host(parts)
    if parts.scheme == "http" and not _is_loopback(host.strip("[]")):
        raise _Refused(
            "The package index URL must use https; http is accepted only for localhost and "
            "loopback addresses."
        )
    path = _canonical_path(parts.path)
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}{path}"


def comparable_index_url(url: str) -> str | None:
    """Reduce an index URL to the one form every spelling of that index shares.

    The rule is the registration rule (`normalize_index_url`): a URL that
    breaks it equals no known source, so a lock cannot reach a credential
    through a spelling Python and uv might read differently.

    Parameters
    ----------
    url
        A URL from a lock, a `pyproject.toml` or a setting.

    Returns
    -------
    str or None
        Scheme and host in lower case, the port only when it is not the
        scheme's default, the path without trailing slashes. `None` when the
        URL breaks any rule of `normalize_index_url`.
    """
    try:
        return _canonical(url)
    except _Refused:
        return None


def without_userinfo(url: str) -> str:
    """Drop the credential from a URL.

    Parameters
    ----------
    url
        A URL, possibly ``scheme://user:password@host/path``.

    Returns
    -------
    str
        The URL without userinfo, query and fragment; the text itself when it
        cannot be parsed.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


def normalize_index_url(url: str, *, credential: bool) -> str:
    """Validate a package index URL for registration and return its stored form.

    The one rule for a URL and whether a credential goes with it: registering
    an index, changing one and the worker's check before it reads a secret all
    call this function.

    A stored URL means the same origin to Python and to uv, and one index has
    one stored form. The URL must be printable ASCII with no backslash; use
    ``https`` (``http`` only for ``localhost`` and loopback addresses); carry
    no user name, password, query or fragment; name an ASCII DNS host or an
    IP address in its canonical spelling; and have a path of segments made of
    letters, digits and ``- . _ ~ + @``, none empty, none ``.`` or ``..``, with
    no ``%``. Six hosts of the public index are refused in every case: PyPI
    must never receive a credential, a lock may name PyPI without registration,
    and the others are not indexes. TestPyPI (``test.pypi.org``) is accepted
    without a credential and refused with one. No message contains the URL: a
    rejected one may carry a credential.

    Parameters
    ----------
    url
        The URL an administrator gave.
    credential
        Whether the index has, or is about to get, a credential.

    Returns
    -------
    str
        `comparable_index_url(url)`.

    Raises
    ------
    ValidationError
        If the URL is longer than 1024 characters, breaks a rule above, is
        PyPI's own, or is TestPyPI's with a credential; the message names the
        rule and what to write.
    """
    try:
        stored = _canonical(url)
    except _Refused as exc:
        raise ValidationError(str(exc)) from None
    host = urlsplit(stored).hostname
    if host in _PUBLIC_HOSTS:
        raise ValidationError(
            "The package index URL is PyPI's own address. PyPI takes no credential and needs "
            f"no registration: a lock may take packages from {PYPI_URL} as it is."
        )
    if host == _TESTPYPI_HOST and credential:
        raise ValidationError(
            "The package index URL is TestPyPI's address. TestPyPI needs no credential for "
            "downloads; register it without one (leave out --credential-secret)."
        )
    return stored


def _operator_key(url: str) -> str | None:
    """Reduce a URL to the form the operator's index is compared in.

    Parameters
    ----------
    url
        The operator's setting or a lock's ``registry`` URL.

    Returns
    -------
    str or None
        Scheme and host in lower case, a port that is not the scheme's default,
        the path without trailing slashes, query and fragment as written; no
        userinfo. `None` for anything that is not an ``http(s)://host`` URL
        Python can split.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), parts.query, parts.fragment))


@dataclass(frozen=True)
class RegisteredIndex:
    """A registered package index, as plain values.

    Parameters
    ----------
    name
        Its name.
    url
        Its URL as stored (`normalize_index_url`).
    credential_secret
        The name of the secret holding its credential, resolved per
        environment; `None` for an index that asks for none.
    id
        The row's id, which a secret's binding names; `None` for an index built by hand,
        which no secret serves.
    """

    name: str
    url: str
    credential_secret: str | None = None
    id: uuid.UUID | None = None


@dataclass(frozen=True)
class KnownSources:
    """Every source a lock may name on this instance.

    Parameters
    ----------
    registered
        The registered package indexes.
    operator_index
        `LAKANA_RUN__INDEX_URL` as set, or `None`. It may carry a password, so
        it is left out of `repr`; `is_known` compares it without userinfo.
    """

    registered: tuple[RegisteredIndex, ...] = ()
    operator_index: str | None = field(default=None, repr=False)

    def by_url(self) -> dict[str, RegisteredIndex]:
        """Index the registered indexes by their stored URL.

        Returns
        -------
        dict
            Stored URL → index.
        """
        return {index.url: index for index in self.registered}

    def is_known(self, url: str) -> bool:
        """Say whether a lock's ``registry`` URL names a source this instance knows.

        The public index and every registered index match through the strict
        rule (`comparable_index_url`). The operator's index matches through a
        lenient comparison, because the setting is operator configuration with
        its own validation (``http``, userinfo, ``+`` and ``%`` in a path): the
        two URLs are equal when they are equal after lower-casing scheme and
        host, dropping a default port, dropping userinfo and dropping trailing
        slashes, and in nothing more. A lock URL that is not printable ASCII or
        holds a backslash is never the operator's index: Python and uv read such
        a URL differently (a backslash before an ``@`` is a path separator to
        uv and part of the userinfo to Python), and Python quietly drops a tab
        or a newline.

        Parameters
        ----------
        url
            The URL as written in the lock.

        Returns
        -------
        bool
            Whether it is the public index, a registered index or the
            operator's index. Never raises.
        """
        comparable = comparable_index_url(url)
        if comparable is not None and (
            comparable == PYPI_URL or any(index.url == comparable for index in self.registered)
        ):
            return True
        if self.operator_index is None or not isinstance(url, str):
            return False
        if not _PRINTABLE_ASCII.fullmatch(url) or "\\" in url:
            return False
        operator = _operator_key(self.operator_index)
        return operator is not None and operator == _operator_key(url)
