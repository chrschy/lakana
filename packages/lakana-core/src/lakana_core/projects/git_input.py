"""Pure validation of the values that reach a git subprocess.

The API server validates at registration and submission; the worker
(`lakana_worker.git`) calls the same functions again before building any
command, so a row written by an older release or edited in the database
cannot smuggle an option into git.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Sequence
from urllib.parse import SplitResult, urlsplit

from lakana_core.errors import ValidationError
from lakana_core.models import RepositoryKind

_FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")
_FULL_SHA_ANY_CASE = re.compile(r"\A[0-9a-fA-F]{40}\Z")
_FORBIDDEN_REF_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_SCP_LIKE = re.compile(r"\A(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>[A-Za-z0-9.-]+):(?P<path>.+)\Z")
_MAX_URL = 2048


def is_full_sha(ref: str) -> bool:
    """Report whether `ref` is a full, lower-case 40-hex commit SHA.

    Parameters
    ----------
    ref
        A validated ref.

    Returns
    -------
    bool
        Whether it names a commit directly.
    """
    return _FULL_SHA.match(ref) is not None


def validate_ref(ref: str) -> str:
    r"""Validate a ref against `git check-ref-format` rules, plus no leading ``-``.

    Parameters
    ----------
    ref
        A branch, tag, full ref name, ``HEAD`` or a 40-hex SHA.

    Returns
    -------
    str
        `ref`, with a full SHA lower-cased.

    Raises
    ------
    ValidationError
        If it is empty, longer than 255 characters, starts with ``-`` or
        ``/``, contains a control character, space, ``~ ^ : ? * [ \``,
        ``..``, ``@{``, ``//``, a component starting with ``.`` or ending
        in ``.lock``, ends with ``/`` or ``.``, or is ``@``; or if it names
        a fully qualified ref outside ``refs/heads/`` and ``refs/tags/``.
        Short names are accepted here; the worker resolves them to a branch
        or tag by exact match.
    """
    if _FULL_SHA_ANY_CASE.match(ref):
        return ref.lower()
    problems = (
        not ref
        or len(ref) > 255
        or ref.startswith(("-", "/"))
        or ref.endswith(("/", "."))
        or ref == "@"
        or _FORBIDDEN_REF_CHARS.search(ref) is not None
        or ".." in ref
        or "@{" in ref
        or "//" in ref
        or any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))
    )
    if problems:
        raise ValidationError(f"{ref[:80]!r} is not a valid git ref.")
    if ref.startswith("refs/") and not ref.startswith(("refs/heads/", "refs/tags/")):
        raise ValidationError(
            f"{ref[:80]!r} is not a branch, a tag or HEAD; only those can be deployed."
        )
    return ref


def _refuse_unsafe_text(url: str) -> None:
    """Refuse URL text that git or a shell could misread.

    Parameters
    ----------
    url
        The URL as given.

    Raises
    ------
    ValidationError
        If it is too long, contains whitespace or a control character, or
        starts with ``-``.
    """
    if len(url) > _MAX_URL:
        raise ValidationError(f"Repository URL is longer than {_MAX_URL} characters.")
    if re.search(r"[\x00-\x20\x7f]", url):
        raise ValidationError("Repository URL must not contain whitespace or control characters.")
    if url.startswith("-"):
        raise ValidationError("Repository URL host must not start with '-'.")


def _split(url: str) -> SplitResult:
    """Parse a URL, turning the parser's `ValueError` into a `ValidationError`.

    Parameters
    ----------
    url
        The URL as given.

    Returns
    -------
    SplitResult
        The parsed URL, whose `port` has been checked to be numeric.

    Raises
    ------
    ValidationError
        If `urlsplit` rejects it (bad IPv6 literal, NFKC-unstable netloc) or
        its port is not a number.
    """
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a non-numeric port
    except ValueError as exc:
        raise ValidationError("Repository URL is malformed.") from exc
    return parts


def validate_repo_url(kind: RepositoryKind, url: str) -> str:
    """Validate a repository URL for its kind.

    Parameters
    ----------
    kind
        The repository's kind.
    url
        ``https://…``; ``ssh://…`` or scp-style ``[user@]host:path``; or,
        for ``local``, an absolute path.

    Returns
    -------
    str
        `url`, unchanged.

    Raises
    ------
    ValidationError
        Wrong scheme for the kind, credentials
        in an HTTPS URL, a password in an SSH URL, a missing host, a host
        or path starting with ``-``, ``?`` or ``#`` in a remote URL,
        whitespace, or a relative local path.
    """
    if kind is RepositoryKind.LOCAL:
        if not os.path.isabs(url):
            raise ValidationError("A local repository URL must be an absolute path.")
        if len(url) > _MAX_URL:
            raise ValidationError(f"Repository URL is longer than {_MAX_URL} characters.")
        if any(unicodedata.category(c) in ("Cc", "Cf") for c in url):
            raise ValidationError("Repository URL must not contain control characters.")
        return url
    _refuse_unsafe_text(url)
    if "?" in url or "#" in url:
        # urlsplit ends the authority at `?`/`#`, git does not: `ssh://a?@b/r` validates
        # host `a` here while git connects to `b` as user `a?`.
        raise ValidationError("Repository URL must not contain '?' or '#'.")
    if kind is RepositoryKind.HTTPS:
        parts = _split(url)
        if parts.scheme != "https":
            raise ValidationError("An https repository URL must start with https://.")
        if "@" in parts.netloc:
            raise ValidationError(
                "An https repository URL must not contain credentials; bind a secret instead."
            )
        if not parts.hostname:
            raise ValidationError("Repository URL has no host.")
        return url
    if url.startswith("ssh://"):
        parts = _split(url)
        if "%" in parts.netloc:
            raise ValidationError("Repository URL host must not be percent-encoded.")
        if parts.password is not None:
            raise ValidationError("An ssh repository URL must not contain a password.")
        if parts.username is not None and parts.username.startswith("-"):
            raise ValidationError("Repository URL user name must not start with '-'.")
        if not parts.hostname or parts.hostname.startswith("-"):
            raise ValidationError("Repository URL host is missing or starts with '-'.")
        if parts.path.lstrip("/").startswith("-"):
            raise ValidationError("Repository URL path must not start with '-'.")
        return url
    scp = _SCP_LIKE.match(url)
    if scp is None or "://" in url or scp["path"].startswith(":"):
        raise ValidationError("An ssh repository URL must be ssh://… or [user@]host:path.")
    if scp["host"].startswith("-"):
        raise ValidationError("Repository URL host must not start with '-'.")
    if scp["path"].startswith("-"):
        raise ValidationError("Repository URL path must not start with '-'.")
    return url


def resolve_local_path(url: str, repo_roots: Sequence[str]) -> str:
    """Resolve a local repository path and confine it to `LAKANA_REPO_ROOTS`.

    Parameters
    ----------
    url
        The absolute path as registered.
    repo_roots
        The configured roots; empty disables local repositories.

    Returns
    -------
    str
        The path with every symlink resolved.

    Raises
    ------
    ValidationError
        If no roots are configured, the resolved path does not lie under (or
        equal) a resolved root (checked first, so an outside path is refused
        the same way whether or not it exists), or, inside a root, it is not
        an existing directory or its last component is ``*`` (the worker's
        `safe.directory` entry would become a prefix glob).
    """
    if not repo_roots:
        raise ValidationError(
            "Local repositories are disabled: LAKANA_REPO_ROOTS is empty. On a server, the "
            "operator sets LAKANA_REPO_ROOTS; a local instance started with "
            "'lakana server start --local' allows the home directory."
        )
    resolved = os.path.realpath(url)
    # The roots come first: a path outside them gets one answer whether or not it exists, so
    # the check cannot be used to probe the server's file system.
    if any(
        resolved == real_root or resolved.startswith(real_root.rstrip(os.sep) + os.sep)
        for real_root in (os.path.realpath(root) for root in repo_roots)
    ):
        if not os.path.isdir(resolved):
            raise ValidationError(
                f"Local repository path {url!r} is not a directory. Check the path."
            )
        if os.path.basename(resolved) == "*":
            raise ValidationError(f"Local repository path {url!r}: last component must not be '*'.")
        return resolved
    # The configured roots are not named: the caller may not be allowed to learn them.
    raise ValidationError(
        f"Local repository path {url!r} is not under any of LAKANA_REPO_ROOTS. "
        "With a local instance (lakana server start --local), move the project under your "
        "home directory. On a server, the operator adds its parent directory to "
        "LAKANA_REPO_ROOTS."
    )
