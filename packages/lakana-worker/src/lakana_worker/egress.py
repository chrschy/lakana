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

The checks, the pinning, the TLS and proxy set-up and the failure reasons are shared with the CI
OIDC fetcher and live in `lakana_core.net.egress`; this module adds the webhook's own reasons,
its allow-list, its test hook and the delivery result.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from lakana_core.config import WebhookConfig
from lakana_core.net import egress as net
from lakana_core.net.egress import (
    CONNECTION_LOST,
    FAILED,
    NO_CONNECTION,
    PROXY_ERROR,
    REFUSED,
    TIMEOUT,
    TLS_ERROR,
    EgressRefused,
    http_libraries_silenced,
    is_internal,
    is_ip_literal,
)

__all__ = [
    "CONNECTION_LOST",
    "FAILED",
    "INVALID",
    "NOT_443",
    "NOT_HTTPS",
    "NO_CONNECTION",
    "PRIVATE",
    "PROXY_ERROR",
    "PROXY_RESOLVE_TIMED_OUT",
    "PROXY_UNRESOLVED",
    "REFUSED",
    "RESOLVE_TIMED_OUT",
    "RESOLVE_TIMEOUT",
    "TIMEOUT",
    "TLS_ERROR",
    "UNRESOLVED",
    "DeliveryResult",
    "Destination",
    "EgressRefused",
    "check_destination",
    "post",
]

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

# Seconds a name lookup may take; inside the 10 s budget of one delivery attempt.
RESOLVE_TIMEOUT: float = 5.0
# The shared lookup pool of `lakana_core.net.egress` and its resolver; the names stay here for
# the tests, which replace `_resolve`.
_DNS = net.DNS
_resolve = net.resolve


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


def _lookup(host: str, port: int, timed_out: str, unresolved: str) -> list[str]:
    """Resolve a name with `_resolve`, giving up after `RESOLVE_TIMEOUT`.

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
    """
    return net.lookup(host, port, timed_out, unresolved, resolver=_resolve, timeout=RESOLVE_TIMEOUT)


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
    scheme, host, explicit_port, target = net.parse_url(url, INVALID)
    exempt = net.host_matches(host, settings.allowed_hosts())
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    if not exempt:
        if scheme != "https":
            raise EgressRefused(NOT_HTTPS)
        if port != 443 and not _TEST_ALLOW_LOOPBACK:
            raise EgressRefused(NOT_443)
    if is_ip_literal(host):
        addresses = [host]
    else:
        addresses = _lookup(host, port, RESOLVE_TIMED_OUT, UNRESOLVED)
    _resolve_proxy(settings)
    parsed = [ipaddress.ip_address(address.split("%")[0]) for address in addresses]
    if not exempt:
        loopback = _TEST_ALLOW_LOOPBACK and all(address.is_loopback for address in parsed)
        if not loopback and any(is_internal(address) for address in parsed):
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
    if not is_ip_literal(host):
        _lookup(host, parts.port or 3128, PROXY_RESOLVE_TIMED_OUT, PROXY_UNRESOLVED)


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
    verify, proxy = net.tls_and_proxy(destination.ca_bundle, destination.proxy)
    url, host_header, extensions = net.pinned_request(
        destination.scheme, destination.host, destination.port, destination.target, address
    )
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
        The last attempt's failure, when no address connects (`net.each_address`).
    """
    if destination.proxy is not None:
        return await _send(destination, None, body, headers, timeout, timeout)

    async def attempt(address: str, remaining: float, share: float) -> int:
        """Send to one address.

        Parameters
        ----------
        address
            The checked address.
        remaining
            Seconds left for each read and write.
        share
            Seconds for connecting to this address.

        Returns
        -------
        int
            The HTTP status.
        """
        return await _send(destination, address, body, headers, remaining, share)

    return await net.each_address(destination.addresses, timeout, attempt)


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
    with http_libraries_silenced():
        try:
            status = net.run_with_deadline(_deliver(destination, body, headers, timeout), timeout)
        except Exception as exc:  # any failure must become a fixed reason
            return DeliveryResult(ok=False, permanent=False, reason=net.classify(exc), status=None)
    reason = f"the server answered {status}"
    if 200 <= status < 300:
        return DeliveryResult(ok=True, permanent=False, reason=reason, status=status)
    if 300 <= status < 400:
        return DeliveryResult(
            ok=False, permanent=True, reason=f"{reason} (a redirect, not followed)", status=status
        )
    permanent = 400 <= status < 500 and status not in (408, 429)
    return DeliveryResult(ok=False, permanent=permanent, reason=reason, status=status)
