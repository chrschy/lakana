"""What the launcher accepts, and the container and network specs it builds from it.

A request is one JSON object with exactly the fields of its operation (`FIELDS`), and every
value is checked against a pattern or a bound before anything else happens. Every Docker field
that matters is fixed here: the image comes from the operator's allow-list, the mounts from the
work volume, the environment volume and the allowed ingestion roots; user, capabilities,
network and command are constants. Nothing the caller sends reaches Docker as-is: identifiers
become names only through the patterns, the stdin text becomes the container's input stream,
and an index credential becomes one entry of `UV_INDEX`.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, NoReturn
from urllib.parse import quote, urlsplit, urlunsplit

from lakana_worker.launcher.settings import LauncherSettings

MAX_REQUEST_BYTES = 512 * 1024
MAX_STDIN_BYTES = 256 * 1024
MAX_INDEXES = 16
MAX_HOSTS = 32
MAX_ROOTS = 16
MAX_CREDENTIAL_CHARS = 4096
MAX_URL_CHARS = 2048
MAX_KILL_GRACE = 300
MAX_ENVIRONMENT_BYTES = 1 << 40
MIN_MEMORY_MB = 64
INDEX_NAME_PREFIX = "lakana-registered-"
"""Prefix of the index names in `UV_INDEX`; the credential is bound to the URL, not the name."""

LABEL_STACK = "lakana.launcher.stack"
LABEL_KIND = "lakana.launcher.kind"
LABEL_ID = "lakana.launcher.id"
LABEL_OWNER = "lakana.launcher.owner"

_ID = re.compile(r"\A[a-z0-9][a-z0-9-]{0,62}\Z")
_OWNER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_KEY = re.compile(r"\A[0-9a-f]{64}\Z")
_ATTEMPT = re.compile(r"\Aattempt-[a-z0-9_]{1,32}\Z")
_HOST = re.compile(
    r"\A(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z"
)
_CONTROL = re.compile(r"[\x00-\x20\x7f]")
_URL_CHARS = re.compile(r"[A-Za-z0-9._~:/@!$&'()*+,;=%\[\]-]+")
_NUMERIC_LABEL = re.compile(r"[0-9]")
MIN_CPUS = 0.01
"""Least CPU share Docker accepts; below it a share rounds to no limit at all."""

_ID_RULE = "lower-case letters, digits and '-', at most 63"
_OWNER_RULE = "letters, digits, '.', '_', ':' and '-', at most 128"
_KEY_RULE = "64 lower-case hex digits"
_ATTEMPT_RULE = "an attempt directory's name"

_LIMITS = frozenset({"memory_mb", "cpus", "pids", "timeout_seconds"})
FIELDS: dict[str, frozenset[str]] = {
    "run": frozenset(
        {
            "token",
            "op",
            "task",
            "owner",
            "python",
            "environment",
            "attempt",
            "kill_grace_seconds",
            "stdin",
            "roots",
        }
    )
    | _LIMITS,
    "build": frozenset(
        {
            "token",
            "op",
            "build",
            "owner",
            "python",
            "environment",
            "attempt",
            "indexes",
            "operator_index",
            "hosts",
            "no_build",
            "max_bytes",
        }
    )
    | _LIMITS,
    "stop": frozenset({"token", "op", "id"}),
    "status": frozenset({"token", "op", "python", "environment"}),
    "cleanup": frozenset({"token", "op", "owner"}),
}
"""The exact fields of each operation."""


class Refused(Exception):
    """A request the launcher does not run; the text names the field and the rule."""


@dataclass(frozen=True)
class Limits:
    """A container's limits, each within the operator's ceiling.

    Parameters
    ----------
    memory_mb
        Memory and memory plus swap, in MiB.
    cpus
        CPUs.
    pids
        Most processes.
    timeout_seconds
        Seconds before the launcher stops the container.
    """

    memory_mb: int
    cpus: float
    pids: int
    timeout_seconds: int


@dataclass(frozen=True)
class IndexCredential:
    """A registered package index a build may present a credential to.

    Parameters
    ----------
    url
        ``https://host[:port]/path``, without userinfo.
    host
        Its host, lower case.
    username
        The user name; may be empty. Left out of `repr`.
    password
        The password. Left out of `repr`.
    """

    url: str
    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class RunRequest:
    """Run one attempt of a Python task.

    Parameters
    ----------
    task
        The attempt's id; names the container and its network.
    owner
        The worker that asked, for `cleanup`.
    python
        The Python version; selects the image.
    environment
        The environment key (hex SHA-256).
    attempt
        The attempt directory's name in the work volume.
    limits
        Memory, CPUs, processes, timeout.
    kill_grace_seconds
        Seconds between SIGTERM and SIGKILL when the container is stopped.
    stdin
        The task description, written to the container's stdin. Left out of `repr`.
    roots
        Allowed ingestion roots to mount read-only.
    """

    task: str
    owner: str
    python: str
    environment: str
    attempt: str
    limits: Limits
    kill_grace_seconds: int
    stdin: bytes = field(repr=False)
    roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildRequest:
    """Build one environment.

    Parameters
    ----------
    build
        The build's id; names the container and its network.
    owner
        The worker that asked.
    python
        The Python version; selects the image.
    environment
        The environment key.
    attempt
        The attempt directory whose ``code`` holds `pyproject.toml` and `uv.lock`.
    indexes
        Registered indexes with a credential.
    operator_index
        `LAKANA_RUN__INDEX_URL`, which may carry its own credential. Left out of `repr`.
    hosts
        Every host the build may reach through the egress proxy.
    no_build
        Whether uv may install wheels only (a credential is in play).
    limits
        Memory, CPUs, processes, timeout.
    max_bytes
        Largest size of the built environment.
    """

    build: str
    owner: str
    python: str
    environment: str
    attempt: str
    indexes: tuple[IndexCredential, ...]
    operator_index: str | None = field(repr=False)
    hosts: tuple[str, ...]
    no_build: bool
    limits: Limits
    max_bytes: int


@dataclass(frozen=True)
class StopRequest:
    """Stop a running task or build.

    Parameters
    ----------
    id
        The task's or build's id.
    """

    id: str


@dataclass(frozen=True)
class StatusRequest:
    """Ask whether an environment is built.

    Parameters
    ----------
    python
        The Python version.
    environment
        The environment key.
    """

    python: str
    environment: str


@dataclass(frozen=True)
class CleanupRequest:
    """Remove a worker's containers that nothing supervises any more.

    Parameters
    ----------
    owner
        The worker.
    """

    owner: str


Request = RunRequest | BuildRequest | StopRequest | StatusRequest | CleanupRequest


def _encodes(text: str) -> bool:
    """Say whether a string can be written as UTF-8.

    JSON accepts a lone surrogate escape, which no UTF-8 encoder takes.

    Parameters
    ----------
    text
        The string.

    Returns
    -------
    bool
        ``False`` for a string with a lone surrogate.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a request object, refusing a field named twice.

    Parameters
    ----------
    pairs
        The key and value pairs in document order.

    Returns
    -------
    dict
        The object.

    Raises
    ------
    Refused
        If a key occurs twice: the last one would silently win.
    """
    result = dict(pairs)
    if len(result) != len(pairs):
        raise Refused("a request repeats the field of a JSON object; send each field once")
    return result


def _no_constant(name: str) -> NoReturn:
    """Refuse `NaN`, `Infinity` and `-Infinity`, which JSON does not have.

    Parameters
    ----------
    name
        The constant.

    Raises
    ------
    Refused
        Always.
    """
    raise Refused(f"a request is one JSON object without {name}")


def _pattern(data: dict[str, Any], key: str, pattern: re.Pattern[str], rule: str) -> str:
    """Read a text field that must match a pattern.

    Parameters
    ----------
    data
        The request.
    key
        The field.
    pattern
        The pattern.
    rule
        The rule in words.

    Returns
    -------
    str
        The value.

    Raises
    ------
    Refused
        If it is not text or does not match.
    """
    value = data[key]
    if not isinstance(value, str) or pattern.match(value) is None:
        raise Refused(f"{key}: {rule}")
    return value


def _raise_with(setting: str | None) -> str:
    """Name the setting that raises a ceiling.

    Parameters
    ----------
    setting
        The variable's name after the prefix, or `None` for a fixed ceiling.

    Returns
    -------
    str
        A parenthesis for a refusal's text, or nothing.
    """
    return f" (LAKANA_LAUNCHER__{setting}; the operator raises it)" if setting else ""


def _integer(
    data: dict[str, Any], key: str, low: int, high: int, setting: str | None = None
) -> int:
    """Read a whole number within bounds; `True` is not a number here.

    Parameters
    ----------
    data
        The request.
    key
        The field.
    low
        The least value.
    high
        The greatest value.
    setting
        The launcher setting that raises `high`, named in the refusal.

    Returns
    -------
    int
        The value.

    Raises
    ------
    Refused
        If it is not an `int` in range.
    """
    value = data[key]
    if type(value) is not int or not low <= value <= high:
        raise Refused(f"{key}: an integer from {low} to {high}" + _raise_with(setting))
    return value


def _limits(data: dict[str, Any], settings: LauncherSettings) -> Limits:
    """Read the four limits.

    Parameters
    ----------
    data
        The request.
    settings
        The ceilings.

    Returns
    -------
    Limits
        The limits.

    Raises
    ------
    Refused
        If one is not a number within its ceiling.
    """
    cpus = data["cpus"]
    if (
        type(cpus) not in (int, float)
        or not math.isfinite(cpus)
        or not MIN_CPUS <= cpus <= settings.max_cpus
    ):
        raise Refused(
            f"cpus: a number from {MIN_CPUS:g} to {settings.max_cpus:g}" + _raise_with("MAX_CPUS")
        )
    return Limits(
        memory_mb=_integer(
            data, "memory_mb", MIN_MEMORY_MB, settings.max_memory_mb, "MAX_MEMORY_MB"
        ),
        cpus=float(cpus),
        pids=_integer(data, "pids", 1, settings.max_pids, "MAX_PIDS"),
        timeout_seconds=_integer(data, "timeout_seconds", 1, settings.max_seconds),
    )


def _python(data: dict[str, Any], settings: LauncherSettings) -> str:
    """Read the Python version, which must name an allowed image.

    Parameters
    ----------
    data
        The request.
    settings
        The allow-list.

    Returns
    -------
    str
        The version.

    Raises
    ------
    Refused
        If no allowed image has it.
    """
    value = data["python"]
    if not isinstance(value, str) or value not in settings.images:
        raise Refused(
            f"python must be one of {', '.join(sorted(settings.images))} (LAKANA_LAUNCHER__IMAGES)"
        )
    return value


def _allowed_host(host: str) -> bool:
    """Say whether a host name may be reached by a build.

    A host name is allowed. A host whose last label starts with a digit is a number, and is
    allowed only as a global unicast IP address: loopback, link-local (the cloud metadata
    address), private, shared (100.64.0.0/10), multicast and unspecified addresses, and the
    short or hex spellings a resolver would turn into one, are not.

    Parameters
    ----------
    host
        A lower-case host from a request.

    Returns
    -------
    bool
        ``True`` if the build may be told to reach it.
    """
    if _HOST.match(host) is None:
        return False
    if _NUMERIC_LABEL.match(host.rsplit(".", 1)[-1]) is None:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_global and not address.is_multicast


def _host_of(url: str, *, schemes: tuple[str, ...], userinfo: bool) -> str | None:
    """Return the host of a URL the launcher will hand to uv, or `None` for a bad one.

    Parameters
    ----------
    url
        The URL.
    schemes
        The schemes allowed.
    userinfo
        Whether a user name and password may be in it.

    Returns
    -------
    str or None
        The lower-case host.
    """
    if len(url) > MAX_URL_CHARS or _CONTROL.search(url) or _URL_CHARS.fullmatch(url) is None:
        return None
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if (
        parts.scheme not in schemes
        or host is None
        or not _allowed_host(host)
        or parts.query
        or parts.fragment
        or (not userinfo and "@" in parts.netloc)
    ):
        return None
    return host


def _credential(item: object) -> IndexCredential:
    """Read one index credential.

    Parameters
    ----------
    item
        An element of ``indexes``.

    Returns
    -------
    IndexCredential
        The credential.

    Raises
    ------
    Refused
        If it is not exactly ``url``, ``username``, ``password`` of the right shapes.
    """
    rule = (
        "a list of at most 16 {url, username, password}: an https URL without userinfo, a user "
        "name and a non-empty password of at most 4096 characters each, no NUL"
    )
    if not isinstance(item, dict) or set(item) != {"url", "username", "password"}:
        raise Refused(f"indexes: {rule}")
    url, username, password = item["url"], item["username"], item["password"]
    host = _host_of(url, schemes=("https",), userinfo=False) if isinstance(url, str) else None
    texts = isinstance(username, str) and isinstance(password, str)
    if host is None or not texts or not password:
        raise Refused(f"indexes: {rule}")
    if any(
        len(v) > MAX_CREDENTIAL_CHARS or "\x00" in v or not _encodes(v)
        for v in (username, password)
    ):
        raise Refused(f"indexes: {rule}")
    return IndexCredential(url=url, host=host, username=username, password=password)


def _run(data: dict[str, Any], settings: LauncherSettings) -> RunRequest:
    """Read a ``run`` request.

    Parameters
    ----------
    data
        The request.
    settings
        The allow-lists and ceilings.

    Returns
    -------
    RunRequest
        The request.

    Raises
    ------
    Refused
        For the first field that breaks its rule.
    """
    stdin = data["stdin"]
    if (
        not isinstance(stdin, str)
        or not _encodes(stdin)
        or len(stdin.encode("utf-8")) > MAX_STDIN_BYTES
    ):
        raise Refused("stdin: a string of at most 256 KiB")
    roots = data["roots"]
    names = ", ".join(sorted(settings.ingestion_roots)) or "none is set"
    root_rule = f"roots: a list of distinct names from LAKANA_LAUNCHER__INGESTION_ROOTS ({names})"
    if (
        not isinstance(roots, list)
        or len(roots) > MAX_ROOTS
        or not all(isinstance(r, str) and r in settings.ingestion_roots for r in roots)
        or len(set(roots)) != len(roots)
    ):
        raise Refused(root_rule)
    return RunRequest(
        task=_pattern(data, "task", _ID, _ID_RULE),
        owner=_pattern(data, "owner", _OWNER, _OWNER_RULE),
        python=_python(data, settings),
        environment=_pattern(data, "environment", _KEY, _KEY_RULE),
        attempt=_pattern(data, "attempt", _ATTEMPT, _ATTEMPT_RULE),
        limits=_limits(data, settings),
        kill_grace_seconds=_integer(data, "kill_grace_seconds", 0, MAX_KILL_GRACE),
        stdin=stdin.encode("utf-8"),
        roots=tuple(roots),
    )


def _build(data: dict[str, Any], settings: LauncherSettings) -> BuildRequest:
    """Read a ``build`` request.

    Parameters
    ----------
    data
        The request.
    settings
        The allow-lists and ceilings.

    Returns
    -------
    BuildRequest
        The request.

    Raises
    ------
    Refused
        For the first field that breaks its rule.
    """
    indexes = data["indexes"]
    if not isinstance(indexes, list) or len(indexes) > MAX_INDEXES:
        raise Refused("indexes: a list of at most 16 credentials")
    credentials = tuple(_credential(item) for item in indexes)
    hosts = data["hosts"]
    if (
        not isinstance(hosts, list)
        or not 1 <= len(hosts) <= MAX_HOSTS
        or not all(isinstance(h, str) and _allowed_host(h) for h in hosts)
        or len(set(hosts)) != len(hosts)
    ):
        raise Refused(
            "hosts: a list of 1 to 32 distinct lower-case host names without a port, or public "
            "IP addresses"
        )
    operator = data["operator_index"]
    operator_host = None
    if operator is not None:
        operator_host = (
            _host_of(operator, schemes=("http", "https"), userinfo=True)
            if isinstance(operator, str)
            else None
        )
        if operator_host is None:
            raise Refused("operator_index: null or an http(s) URL without spaces")
    if any(c.host not in hosts for c in credentials) or (
        operator_host is not None and operator_host not in hosts
    ):
        raise Refused("indexes: every index's host must be in hosts")
    if type(data["no_build"]) is not bool:
        raise Refused("no_build: true or false")
    return BuildRequest(
        build=_pattern(data, "build", _ID, _ID_RULE),
        owner=_pattern(data, "owner", _OWNER, _OWNER_RULE),
        python=_python(data, settings),
        environment=_pattern(data, "environment", _KEY, _KEY_RULE),
        attempt=_pattern(data, "attempt", _ATTEMPT, _ATTEMPT_RULE),
        indexes=credentials,
        operator_index=operator,
        hosts=tuple(hosts),
        no_build=data["no_build"],
        limits=_limits(data, settings),
        max_bytes=_integer(data, "max_bytes", 1, MAX_ENVIRONMENT_BYTES),
    )


def parse_request(raw: bytes, *, token: str, settings: LauncherSettings) -> Request:
    """Read one request, refusing anything outside the schema.

    Parameters
    ----------
    raw
        The request line as read from the socket.
    token
        The launcher's token; the request must carry it.
    settings
        The allow-lists and ceilings.

    Returns
    -------
    Request
        The parsed request.

    Raises
    ------
    Refused
        For an oversized or malformed line, a wrong token, an unknown operation, a field too
        many or too few, or the first field that breaks its rule.
    """
    if len(raw) > MAX_REQUEST_BYTES:
        raise Refused("a request is one JSON line of at most 512 KiB")
    try:
        data = json.loads(
            raw.decode("utf-8"), parse_constant=_no_constant, object_pairs_hook=_unique_pairs
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused("a request is one JSON object") from None
    if not isinstance(data, dict):
        raise Refused("a request is one JSON object")
    sent = data.get("token")
    if (
        not isinstance(sent, str)
        or not _encodes(sent)
        or not hmac.compare_digest(sent.encode("utf-8"), token.encode("utf-8"))
    ):
        raise Refused(
            "the request does not carry the launcher's token; LAKANA_RUN__LAUNCHER_SOCKET "
            "must point into the launcher's own volume"
        )
    op = data.get("op")
    if not isinstance(op, str) or op not in FIELDS:
        raise Refused(f"op must be one of {', '.join(sorted(FIELDS))}")
    if set(data) != FIELDS[op]:
        raise Refused(f"{op} takes exactly the fields {', '.join(sorted(FIELDS[op]))}")
    if op == "run":
        return _run(data, settings)
    if op == "build":
        return _build(data, settings)
    if op == "stop":
        return StopRequest(_pattern(data, "id", _ID, _ID_RULE))
    if op == "status":
        return StatusRequest(
            python=_python(data, settings),
            environment=_pattern(data, "environment", _KEY, _KEY_RULE),
        )
    return CleanupRequest(_pattern(data, "owner", _OWNER, _OWNER_RULE))


def container_name(settings: LauncherSettings, kind: str, ident: str) -> str:
    """Name a container or network the launcher creates.

    Parameters
    ----------
    settings
        For the stack.
    kind
        ``task``, ``build`` or ``probe``.
    ident
        The id from a request (already checked) or a fixed word.

    Returns
    -------
    str
        ``<stack>-<kind>-<ident>``.
    """
    return f"{settings.stack}-{kind}-{ident}"


def labels(settings: LauncherSettings, kind: str, ident: str, owner: str) -> dict[str, str]:
    """Label what the launcher creates, so a restart and `cleanup` find it.

    Parameters
    ----------
    settings
        For the stack.
    kind
        ``task``, ``build`` or ``probe``.
    ident
        Its id.
    owner
        The worker that asked.

    Returns
    -------
    dict
        The four labels.
    """
    return {LABEL_STACK: settings.stack, LABEL_KIND: kind, LABEL_ID: ident, LABEL_OWNER: owner}


def _digest(*parts: str) -> str:
    """Hash parts into a name-safe key.

    Parameters
    ----------
    *parts
        Texts, joined by newlines.

    Returns
    -------
    str
        The first 40 hex digits of their SHA-256.
    """
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:40]


def environment_volume(settings: LauncherSettings, python: str, key: str) -> str:
    """Name the volume of one built environment.

    The image is part of the name: a venv's `bin/python` is an absolute link into the image
    it was built in, so a new image needs a new build.

    Parameters
    ----------
    settings
        For the stack and the image.
    python
        An allowed Python version.
    key
        The environment key from the worker.

    Returns
    -------
    str
        ``<stack>-env-<digest>``.
    """
    return f"{settings.stack}-env-{_digest(settings.images[python], key)}"


def cache_volume(settings: LauncherSettings, request: BuildRequest) -> str:
    """Name the uv cache of one set of package indexes.

    Builds that may reach the same hosts share it; it is never mounted into a task.

    Parameters
    ----------
    settings
        For the stack.
    request
        The build.

    Returns
    -------
    str
        ``<stack>-uvcache-<digest>``.
    """
    operator = request.operator_index or ""
    if operator:
        parts = urlsplit(operator)
        operator = urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
    digest = _digest(operator, *sorted(request.hosts), *sorted(c.url for c in request.indexes))
    return f"{settings.stack}-uvcache-{digest}"


def network_spec(settings: LauncherSettings, kind: str, ident: str, owner: str) -> dict[str, Any]:
    """Describe the network of one task or build.

    Internal, so nothing routes out of it, and with an isolated gateway: a default internal
    bridge still reaches every host service listening on all interfaces through its gateway
    address (measured in the design trial).

    Parameters
    ----------
    settings
        For the stack.
    kind
        ``task`` or ``build``.
    ident
        Its id.
    owner
        The worker that asked.

    Returns
    -------
    dict
        The body of ``POST /networks/create``.
    """
    return {
        "Name": container_name(settings, kind, ident),
        "Driver": "bridge",
        "Internal": True,
        "Attachable": False,
        "EnableIPv6": False,
        "Labels": labels(settings, kind, ident, owner),
        "Options": {
            "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
            "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
        },
    }


def _volume(
    source: str, target: str, *, read_only: bool, subpath: str | None = None
) -> dict[str, Any]:
    """Describe one volume mount.

    Parameters
    ----------
    source
        The volume's name.
    target
        Where it appears.
    read_only
        Whether it is read-only.
    subpath
        The directory inside the volume, for the work volume; Docker refuses one that leaves the
        volume through ``..`` or a link.

    Returns
    -------
    dict
        One entry of ``HostConfig.Mounts``. A new volume without a subpath is filled from the
        image's directory at `target` once, which gives it the image's owner.
    """
    options: dict[str, Any] = {"NoCopy": subpath is not None}
    if subpath is not None:
        options["Subpath"] = subpath
    return {
        "Type": "volume",
        "Source": source,
        "Target": target,
        "ReadOnly": read_only,
        "VolumeOptions": options,
    }


def _host_config(
    limits: Limits,
    network: str,
    mounts: list[dict[str, Any]],
    tmpfs: str,
    *,
    no_new_privileges: bool,
) -> dict[str, Any]:
    """Describe what every container of the launcher shares.

    Parameters
    ----------
    limits
        The limits.
    network
        Its own network.
    mounts
        Its mounts.
    tmpfs
        Options of the private `/tmp`.
    no_new_privileges
        Whether Docker allows the option here (`Launcher.start` found out).

    Returns
    -------
    dict
        ``HostConfig``.
    """
    memory = limits.memory_mb * 1024 * 1024
    return {
        "Mounts": mounts,
        "Binds": [],
        "Devices": [],
        "Memory": memory,
        "MemorySwap": memory,
        "NanoCpus": round(limits.cpus * 1_000_000_000),
        "PidsLimit": limits.pids,
        "CapDrop": ["ALL"],
        "CapAdd": [],
        "Privileged": False,
        "ReadonlyRootfs": True,
        "Init": True,
        "IpcMode": "private",
        "PidMode": "",
        "UsernsMode": "",
        "CgroupnsMode": "private",
        "NetworkMode": network,
        "PublishAllPorts": False,
        "Tmpfs": {"/tmp": tmpfs},
        "LogConfig": {"Type": "none"},
        "AutoRemove": False,
        "RestartPolicy": {"Name": "no"},
        "SecurityOpt": ["no-new-privileges"] if no_new_privileges else [],
    }


def run_container_spec(
    request: RunRequest, settings: LauncherSettings, *, no_new_privileges: bool
) -> dict[str, Any]:
    """Describe the container of one task attempt.

    Parameters
    ----------
    request
        The run.
    settings
        Images, work volume, user and roots.
    no_new_privileges
        Whether to set the option.

    Returns
    -------
    dict
        The body of ``POST /containers/create``.
    """
    memory = request.limits.memory_mb * 1024 * 1024
    polars = str(max(memory // 1_000_000, 1))
    mounts = [
        _volume(settings.work_volume, "/code", read_only=True, subpath=f"{request.attempt}/code"),
        _volume(
            environment_volume(settings, request.python, request.environment),
            "/env",
            read_only=True,
        ),
        _volume(settings.work_volume, "/work", read_only=False, subpath=f"{request.attempt}/task"),
        *(
            {
                "Type": "bind",
                "Source": settings.ingestion_roots[name],
                "Target": f"/ingest/{name}",
                "ReadOnly": True,
                "BindOptions": {"Propagation": "rprivate", "NonRecursive": True},
            }
            for name in request.roots
        ),
    ]
    return {
        "Image": settings.images[request.python],
        "Entrypoint": ["/env/bin/python", "-P", "-m", "lakana.runner"],
        "Cmd": [],
        "User": settings.task_user,
        "WorkingDir": "/code",
        "Env": [
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "HOME=/work/home",
            "TMPDIR=/work/tmp",
            "PYTHONUNBUFFERED=1",
            "PYTHONDONTWRITEBYTECODE=1",
            "POLARS_OOC_SPILL_DIR=/work/tmp/polars-spill",
            f"POLARS_OVERRIDE_TOTAL_MEMORY_MB={polars}",
            f"POLARS_OOC_DISK_BUDGET_MB={polars}",
        ],
        "AttachStdin": True,
        "AttachStdout": True,
        "AttachStderr": True,
        "OpenStdin": True,
        "StdinOnce": True,
        "Tty": False,
        "NetworkDisabled": False,
        "Labels": labels(settings, "task", request.task, request.owner),
        "HostConfig": _host_config(
            request.limits,
            container_name(settings, "task", request.task),
            mounts,
            "rw,noexec,nosuid,nodev,size=64m",
            no_new_privileges=no_new_privileges,
        ),
    }


def uv_index_value(request: BuildRequest) -> str | None:
    """Build the build's `UV_INDEX`: the operator's index, then each credentialed index.

    uv keeps a credential written into an index URL by scheme, host and port and sends it to
    nothing else, whatever the project's files say.

    Parameters
    ----------
    request
        The build.

    Returns
    -------
    str or None
        Space-separated entries; `None` when there are none.
    """
    entries = [request.operator_index] if request.operator_index else []
    for number, index in enumerate(request.indexes):
        parts = urlsplit(index.url)
        userinfo = f"{quote(index.username, safe='')}:{quote(index.password, safe='')}"
        url = urlunsplit((parts.scheme, f"{userinfo}@{parts.netloc}", parts.path, "", ""))
        entries.append(f"{INDEX_NAME_PREFIX}{number}={url}")
    return " ".join(entries) or None


def build_container_spec(
    request: BuildRequest,
    settings: LauncherSettings,
    *,
    no_new_privileges: bool,
    proxy_url: str | None,
) -> dict[str, Any]:
    """Describe the container that builds one environment.

    It mounts the attempt's code read-only (the lock may name wheels in the repository), its
    own environment volume and the cache of its index set, and runs the image's build entry.

    Parameters
    ----------
    request
        The build.
    settings
        Images, work volume, user.
    no_new_privileges
        Whether to set the option.
    proxy_url
        The egress proxy with this build's token; `None` leaves the build without a way out.

    Returns
    -------
    dict
        The body of ``POST /containers/create``.
    """
    env = [
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "HOME=/tmp",
        "TMPDIR=/tmp",
        "UV_PROJECT_ENVIRONMENT=/env",
        "UV_PYTHON_DOWNLOADS=never",
        "UV_CACHE_DIR=/uv-cache",
        "UV_NO_PROGRESS=1",
        f"LAKANA_BUILD_NO_BUILD={1 if request.no_build else 0}",
        f"LAKANA_BUILD_MAX_BYTES={request.max_bytes}",
    ]
    index = uv_index_value(request)
    if index is not None:
        env.append(f"UV_INDEX={index}")
    if proxy_url is not None:
        env += [
            f"{name}={proxy_url}"
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
        ]
    mounts = [
        _volume(settings.work_volume, "/src", read_only=True, subpath=f"{request.attempt}/code"),
        _volume(
            environment_volume(settings, request.python, request.environment),
            "/env",
            read_only=False,
        ),
        _volume(cache_volume(settings, request), "/uv-cache", read_only=False),
    ]
    return {
        "Image": settings.images[request.python],
        "Entrypoint": ["/usr/local/bin/python", "-I", "/opt/lakana-task/build.py"],
        "Cmd": [],
        "User": settings.build_user,
        "WorkingDir": "/src",
        "Env": env,
        "AttachStdout": True,
        "AttachStderr": True,
        "OpenStdin": False,
        "Tty": False,
        "Labels": labels(settings, "build", request.build, request.owner),
        "HostConfig": _host_config(
            request.limits,
            container_name(settings, "build", request.build),
            mounts,
            "rw,nosuid,nodev,size=1g",
            no_new_privileges=no_new_privileges,
        ),
    }


def probe_container_spec(settings: LauncherSettings, image: str) -> dict[str, Any]:
    """Describe the container that finds out whether `no-new-privileges` works here.

    Parameters
    ----------
    settings
        For the user and the labels.
    image
        An allowed image.

    Returns
    -------
    dict
        The body of ``POST /containers/create``: ``python -c pass`` with the option, no
        network, no mounts.
    """
    host = _host_config(
        Limits(memory_mb=MIN_MEMORY_MB, cpus=0.5, pids=16, timeout_seconds=60),
        "none",
        [],
        "rw,noexec,nosuid,nodev,size=1m",
        no_new_privileges=True,
    )
    return {
        "Image": image,
        "Entrypoint": ["/usr/local/bin/python", "-c", "pass"],
        "Cmd": [],
        "User": settings.task_user,
        "NetworkDisabled": True,
        "Labels": labels(settings, "probe", "no-new-privileges", "launcher"),
        "HostConfig": host,
    }
