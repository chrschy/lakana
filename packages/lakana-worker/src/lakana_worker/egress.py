"""The webhook client: it refuses internal addresses and never keeps or quotes a URL.

A webhook URL comes from a secret that someone with the deploy right named, so the worker treats
it as hostile: an attacker who can name a secret could otherwise point the worker at the cloud
metadata service or at an internal admin interface.

- `check_destination` parses the URL, requires ``https`` and port 443, resolves the name **once**,
  checks **every** address, and returns the address to connect to.
- `post` connects to that address, not to the name, so DNS cannot answer differently between the
  check and the connection (rebinding). It asks for TLS with the name as the server name, so the
  certificate is verified against the name, not the address.
- No redirects are followed and no environment variable is read: ``trust_env=False``, so
  ``HTTPS_PROXY``, ``SSL_CERT_FILE`` and the like change nothing.
- Every failure is reduced to a fixed reason. The text of an `httpx` exception embeds the URL, so
  it is never kept, logged or chained.

Operator settings (`lakana_core.config.WebhookConfig`) can allow named hosts, route through an
explicit proxy and replace the CA store.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import ipaddress
import logging
import re
import socket
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from lakana_core.config import WebhookConfig

# Only the pytest fixture `allow_loopback` ever sets this (`test_egress.py` greps the source tree
# to keep it that way). It admits loopback addresses, and a port other than 443 on them.
_TEST_ALLOW_LOOPBACK: bool = False

RESOLVE_TIMED_OUT = "the webhook host name did not resolve in time"
PROXY_RESOLVE_TIMED_OUT = "the proxy host name did not resolve in time"
PROXY_UNRESOLVED = "the proxy host name does not resolve (check LAKANA_WEBHOOK__PROXY)"
INVALID = "the webhook address is not a valid https URL"
_ALLOW_HINT = "(the operator can allow a host in LAKANA_WEBHOOK__ALLOW_HOSTS)"
NOT_HTTPS = f"the webhook address must use https {_ALLOW_HINT}"
NOT_443 = f"the webhook address must use port 443 {_ALLOW_HINT}"
UNRESOLVED = "the webhook host name does not resolve"
PRIVATE = f"the webhook host is a private or internal address {_ALLOW_HINT}"
TIMEOUT = "timeout"
REFUSED = "connection refused"
TLS_ERROR = "TLS error"
NO_CONNECTION = "could not connect"
CONNECTION_LOST = "connection lost"
PROXY_ERROR = "proxy error"
FAILED = "the request failed"

# Seconds a name lookup may take; inside the 10 s budget of one delivery attempt.
RESOLVE_TIMEOUT: float = 5.0
# A lookup that times out keeps its thread until the system resolver gives up, so spare threads
# keep a few slow names from blocking the next delivery.
_DNS = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="egress-dns")


_HTTPCORE_PARTS = ("connection", "http11", "http2", "proxy", "socks")


_SENDING: contextvars.ContextVar[bool] = contextvars.ContextVar("egress_sending", default=False)


class _SilenceWhileSending(logging.Filter):
    """Drop every record of the http libraries while a webhook is being sent."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Decide whether a record may pass.

        Parameters
        ----------
        record
            The record being logged.

        Returns
        -------
        bool
            `False` while a webhook is sent, so nothing of the URL reaches any handler.
        """
        return not _SENDING.get()


_SILENCER = _SilenceWhileSending()


def _silence_http_libraries() -> None:
    """Attach the silencing filter to every logger of `httpx` and `httpcore`.

    `httpx` logs every request's full URL at INFO and `httpcore` traces host names at DEBUG;
    the path and query of a webhook URL are its secret. A filter on a parent logger does not see
    the records of a child such as `httpcore.connection`, so the filter sits on every logger of
    both libraries. It only acts while `post` runs; other code's `httpx` logging is untouched.
    """
    # httpcore creates its child loggers when it imports its connection modules, which happens
    # on the first request; naming them here attaches the filter to the very objects it will
    # fetch later.
    known = ("httpx", "httpcore", *(f"httpcore.{part}" for part in _HTTPCORE_PARTS))
    names = [n for n in logging.root.manager.loggerDict if n.split(".")[0] in ("httpx", "httpcore")]
    for name in {*known, *names}:
        logging.getLogger(name).addFilter(_SILENCER)


_HOST = re.compile(r"^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*$")
_MAX_HOST = 253

_REFUSED_NETWORKS = tuple(
    ipaddress.ip_network(text)
    for text in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "240.0.0.0/4",
        "::/128",
        "100::/64",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class EgressRefused(Exception):
    """The webhook address may not be used.

    Parameters
    ----------
    reason
        One of this module's fixed reason strings; it never contains the URL.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, repr=False)
class Destination:
    """A checked webhook destination.

    Its text form hides every field, because the URL and the proxy are secrets.

    Parameters
    ----------
    scheme
        ``https``, or ``http`` for an allowed host.
    host
        The lower-case host name without a trailing dot, or the IP literal.
    port
        The port.
    target
        Path and query, starting with ``/``.
    addresses
        The checked addresses, in the system's order; the first that connects is used.
    proxy
        The operator's proxy, or `None`.
    ca_bundle
        The operator's CA file, or `None`.
    """

    scheme: str
    host: str
    port: int
    target: str
    addresses: tuple[str, ...]
    proxy: SecretStr | None
    ca_bundle: Path | None

    def __repr__(self) -> str:
        """Hide the URL.

        Returns
        -------
        str
            A fixed text.
        """
        return "Destination(<redacted>)"


@dataclass(frozen=True)
class DeliveryResult:
    """The outcome of one attempt.

    Parameters
    ----------
    ok
        Whether the server answered 2xx.
    permanent
        Whether retrying cannot help (a 4xx other than 408 and 429, or a redirect).
    reason
        A fixed description; it never contains the URL.
    status
        The HTTP status, or `None` when there was no answer.
    """

    ok: bool
    permanent: bool
    reason: str
    status: int | None


def _embedded_ipv4(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> list[ipaddress.IPv4Address]:
    """Return the IPv4 addresses an IPv6 address carries.

    Parameters
    ----------
    address
        Any address.

    Returns
    -------
    list[ipaddress.IPv4Address]
        The IPv4-mapped, IPv4-compatible, 6to4, Teredo and NAT64 embedded addresses; empty for
        IPv4 and for IPv6 addresses that carry none.
    """
    if not isinstance(address, ipaddress.IPv6Address):
        return []
    found: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        found.extend(address.teredo)
    if address in _NAT64 or int(address) >> 32 == 0:
        found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return found


def _is_internal(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Say whether an address must not be reached.

    Parameters
    ----------
    address
        Any address.

    Returns
    -------
    bool
        `True` for private, loopback, link-local, multicast, reserved, unspecified, CGNAT,
        documentation and benchmarking addresses, unique-local IPv6, anything the standard
        library does not call global, and any IPv6 address that carries such an IPv4 address.
    """
    embedded = _embedded_ipv4(address)
    if any(_is_internal(inner) for inner in embedded):
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
        or any(
            address in network
            for network in _REFUSED_NETWORKS
            if network.version == address.version
        )
    )


def _resolve(host: str, port: int) -> list[str]:
    """Resolve a name to its addresses.

    Parameters
    ----------
    host
        The name.
    port
        The port, only for the lookup.

    Returns
    -------
    list[str]
        The distinct addresses, in the order the system gave them.
    """
    # The trailing dot makes the name absolute: no search domain is tried first.
    infos = socket.getaddrinfo(host + ".", port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _lookup(host: str, port: int, timed_out: str, unresolved: str) -> list[str]:
    """Resolve a name on a helper thread, giving up after `RESOLVE_TIMEOUT`.

    Parameters
    ----------
    host
        The name.
    port
        The port, only for the lookup.
    timed_out
        The fixed reason when the lookup does not answer in time.
    unresolved
        The fixed reason when the name does not resolve.

    Returns
    -------
    list[str]
        The addresses.

    Raises
    ------
    EgressRefused
        If the lookup times out or finds nothing.
    """
    future = _DNS.submit(_resolve, host, port)
    failure = ""
    addresses: list[str] = []
    try:
        addresses = future.result(timeout=RESOLVE_TIMEOUT)
    except concurrent.futures.TimeoutError:
        failure = timed_out
    except OSError:
        failure = unresolved
    if failure:
        future.cancel()
        raise EgressRefused(failure)
    if not addresses:
        raise EgressRefused(unresolved)
    return addresses


def _matches(host: str, allowed: tuple[str, ...]) -> bool:
    """Say whether a host is on the operator's allow-list.

    Parameters
    ----------
    host
        The normalised host name.
    allowed
        The normalised allow-list.

    Returns
    -------
    bool
        `True` for an exact entry, or for a subdomain of a ``*.domain`` entry.
    """
    for entry in allowed:
        if entry.startswith("*."):
            if host.endswith(entry[1:]) and len(host) > len(entry) - 1:
                return True
        elif host == entry:
            return True
    return False


def _parse(url: str) -> tuple[str, str, int | None, str]:
    """Split a webhook URL into scheme, host, port and target.

    Parameters
    ----------
    url
        The URL.

    Returns
    -------
    tuple[str, str, int | None, str]
        The scheme, the host (lower case, no trailing dot, IDNA-encoded), the explicit port or
        `None`, and the path with query.

    Raises
    ------
    EgressRefused
        If the URL is malformed, has a user name or password, or names a host that is neither a
        DNS name nor an IP literal in its full spelling.
    """
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise EgressRefused(INVALID)
    # Every refusal is raised outside the `except` blocks, so that no exception is chained to it.
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        parts, port, host = None, None, None
    if parts is None or host is None:
        raise EgressRefused(INVALID)
    if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc or port == 0:
        raise EgressRefused(INVALID)
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if not literal:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            host = ""
        # A last label of digits only is a shorthand IPv4 spelling (`127.1`, `2130706433`) that
        # some resolvers expand; no real top-level domain looks like that.
        if not _HOST.match(host) or len(host) > _MAX_HOST or host.rsplit(".", 1)[-1].isdigit():
            raise EgressRefused(INVALID)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return parts.scheme, host, port, target


def check_destination(url: str, settings: WebhookConfig) -> Destination:
    """Check a webhook URL and choose the address to connect to.

    Parameters
    ----------
    url
        The webhook URL. It is never kept in the result's text form, nor in an error.
    settings
        The operator's allow-list, proxy and CA bundle.

    Returns
    -------
    Destination
        Where to send the request.

    Raises
    ------
    EgressRefused
        If the URL is malformed, is not ``https`` on port 443, does not resolve, or any address
        of its host is internal. A host on the allow-list is exempt from the scheme, port and
        address rules. The reason names the rule, never the URL.
    """
    scheme, host, explicit_port, target = _parse(url)
    exempt = _matches(host, settings.allowed_hosts())
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    if not exempt:
        if scheme != "https":
            raise EgressRefused(NOT_HTTPS)
        if port != 443 and not _TEST_ALLOW_LOOPBACK:
            raise EgressRefused(NOT_443)
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = _lookup(host, port, RESOLVE_TIMED_OUT, UNRESOLVED)
    _resolve_proxy(settings)
    parsed = [ipaddress.ip_address(address.split("%")[0]) for address in addresses]
    if not exempt:
        loopback = _TEST_ALLOW_LOOPBACK and all(address.is_loopback for address in parsed)
        if not loopback and any(_is_internal(address) for address in parsed):
            raise EgressRefused(PRIVATE)
        if port != 443 and not loopback:
            raise EgressRefused(NOT_443)
    return Destination(
        scheme=scheme,
        host=host,
        port=port,
        target=target,
        addresses=tuple(addresses),
        proxy=settings.proxy,
        ca_bundle=settings.ca_bundle,
    )


def _resolve_proxy(settings: WebhookConfig) -> None:
    """Look up the proxy's name under the same deadline as a webhook's name.

    The lookup happens here, before the connection, so the connection itself resolves nothing;
    one attempt can still take up to about 20 s in all (the proxy's lookup, the webhook's lookup
    and the 10 s of the request), which is inside a delivery's 60 s lease.

    Parameters
    ----------
    settings
        The operator settings.

    Raises
    ------
    EgressRefused
        If the proxy's name does not resolve in time or at all.
    """
    if settings.proxy is None:
        return
    parts = urlsplit(settings.proxy.get_secret_value())
    host = parts.hostname or ""  # the setting is validated, so a host is always there
    try:
        ipaddress.ip_address(host)
    except ValueError:
        _lookup(host, parts.port or 3128, PROXY_RESOLVE_TIMED_OUT, PROXY_UNRESOLVED)


def _netloc(host: str, port: int) -> str:
    """Format a host and port for a URL or a Host header.

    Parameters
    ----------
    host
        A name or an IP literal.
    port
        The port.

    Returns
    -------
    str
        ``host:port``, with an IPv6 literal in brackets.
    """
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _chain(exc: BaseException) -> list[BaseException]:
    """List an exception with its causes and contexts.

    Parameters
    ----------
    exc
        The exception.

    Returns
    -------
    list[BaseException]
        The exception and everything it was raised from or during.
    """
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def _reason(exc: BaseException) -> str:
    """Reduce any exception to one of the fixed reasons.

    Parameters
    ----------
    exc
        What the request raised. Its text is never read.

    Returns
    -------
    str
        A fixed reason.
    """
    chain = _chain(exc)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return TIMEOUT
    if isinstance(exc, httpx.ProxyError):
        return PROXY_ERROR
    if any(isinstance(inner, ssl.SSLError) for inner in chain):
        return TLS_ERROR
    if any(isinstance(inner, ConnectionRefusedError) for inner in chain):
        return REFUSED
    if isinstance(exc, httpx.ConnectError):
        return NO_CONNECTION
    if isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return CONNECTION_LOST
    return FAILED


async def _send(
    destination: Destination,
    address: str | None,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
    connect_timeout: float,
) -> int:
    """Send the request and return the status, without reading the answer's body.

    Parameters
    ----------
    destination
        The checked destination.
    address
        The checked address to connect to; `None` when going through the proxy.
    body
        The request body.
    headers
        Request headers; ``Host`` is set from the destination.
    timeout
        Seconds for each read and write.
    connect_timeout
        Seconds for connecting and the TLS handshake.

    Returns
    -------
    int
        The HTTP status.
    """
    verify: ssl.SSLContext | bool = True
    if destination.ca_bundle is not None:
        verify = ssl.create_default_context(cafile=str(destination.ca_bundle))
    proxy: httpx.Proxy | None = None
    if destination.proxy is not None:
        # The bundle also covers an https proxy's own certificate.
        proxy_url = destination.proxy.get_secret_value()
        # httpcore refuses a TLS context for a plain-http proxy.
        proxy_tls = (
            verify
            if isinstance(verify, ssl.SSLContext) and urlsplit(proxy_url).scheme.lower() == "https"
            else None
        )
        proxy = httpx.Proxy(proxy_url, ssl_context=proxy_tls)
    host_header = destination.host if ":" not in destination.host else f"[{destination.host}]"
    if destination.port != (443 if destination.scheme == "https" else 80):
        host_header = f"{host_header}:{destination.port}"
    extensions: dict[str, object] = {}
    if address is not None:
        # Connect to the checked address, ask for the name: the certificate is verified against
        # the server name, never against the address.
        netloc = _netloc(address, destination.port)
        if destination.scheme == "https":
            extensions["sni_hostname"] = destination.host
    else:
        # A proxy resolves the name itself and cannot be pinned; it is trusted to do so.
        netloc = _netloc(destination.host, destination.port)
    url = f"{destination.scheme}://{netloc}{destination.target}"
    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        verify=verify,
        proxy=proxy,
        timeout=httpx.Timeout(timeout, connect=connect_timeout),
    ) as client:
        merged = httpx.Headers(headers)
        merged["Host"] = host_header  # replaces any Host the caller sent, whatever its case
        request = client.build_request(
            "POST", url, content=body, headers=merged, extensions=extensions
        )
        response = await client.send(request, stream=True)
        try:
            return response.status_code
        finally:
            await response.aclose()


async def _deliver(
    destination: Destination, body: bytes, headers: dict[str, str], timeout: float
) -> int:
    """Try each checked address in turn until one connects, within one time budget.

    Parameters
    ----------
    destination
        The checked destination.
    body
        The request body.
    headers
        Request headers.
    timeout
        Seconds for the whole attempt.

    Returns
    -------
    int
        The HTTP status.

    Raises
    ------
    Exception
        The last attempt's failure, when no address connects.
    AssertionError
        Never in practice: a destination has at least one address.
    """
    if destination.proxy is not None:
        return await _send(destination, None, body, headers, timeout, timeout)
    deadline = time.monotonic() + timeout
    last = len(destination.addresses) - 1
    for index, address in enumerate(destination.addresses):
        remaining = max(deadline - time.monotonic(), 0.001)
        # Each address gets its share of what is left, so a silent one cannot use it all.
        share = remaining / (last - index + 1)
        try:
            return await _send(destination, address, body, headers, remaining, share)
        except Exception as exc:
            # Only a connection that never came up moves on; a certificate that fails to verify
            # or an answer that breaks off ends the attempt.
            if index == last or not (
                isinstance(exc, httpx.ConnectTimeout) or _reason(exc) in (REFUSED, NO_CONNECTION)
            ):
                raise
    raise AssertionError("unreachable: a destination has at least one address")  # pragma: no cover


def post(
    destination: Destination, body: bytes, headers: dict[str, str], *, timeout: float
) -> DeliveryResult:
    """Send one attempt and classify the answer.

    Parameters
    ----------
    destination
        What `check_destination` returned.
    body
        The request body.
    headers
        Request headers.
    timeout
        Seconds for the whole attempt, connection and answer together.

    Returns
    -------
    DeliveryResult
        Success on 2xx; permanent on a redirect and on a 4xx other than 408 and 429; otherwise a
        retryable failure. A failure carries one of this module's fixed reasons, never the text
        of the exception, which would quote the URL.
    """
    # Not `asyncio.run`: it waits for the loop's default executor, where a proxy name's lookup
    # may still be running, and the attempt must end at its deadline.
    # The filter is attached again before every attempt: loggers created since the last one
    # (httpcore's children) must carry it too.
    _silence_http_libraries()
    token = _SENDING.set(True)
    loop = asyncio.new_event_loop()
    try:
        status = loop.run_until_complete(
            asyncio.wait_for(_deliver(destination, body, headers, timeout), timeout)
        )
    except Exception as exc:  # any failure must become a fixed reason
        return DeliveryResult(ok=False, permanent=False, reason=_reason(exc), status=None)
    finally:
        loop.close()
        _SENDING.reset(token)
    reason = f"the server answered {status}"
    if 200 <= status < 300:
        return DeliveryResult(ok=True, permanent=False, reason=reason, status=status)
    if 300 <= status < 400:
        return DeliveryResult(
            ok=False, permanent=True, reason=f"{reason} (a redirect, not followed)", status=status
        )
    permanent = 400 <= status < 500 and status not in (408, 429)
    return DeliveryResult(ok=False, permanent=permanent, reason=reason, status=status)
