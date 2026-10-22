"""The egress proxy: CONNECT with a valid token to one of its host:port pairs, and nothing else."""

from __future__ import annotations

import base64
import ipaddress
import select
import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from lakana_worker.launcher import egress
from lakana_worker.launcher.egress import (
    EgressServer,
    EgressSettings,
    SettingsError,
    connected_networks,
    load_settings,
)
from lakana_worker.launcher.egress_token import ensure_key, mint, rotate_key

LOOPBACK = ipaddress.ip_network("127.0.0.0/8")


class _Echo(socketserver.BaseRequestHandler):
    """Echoes what it receives, standing in for an index."""

    def handle(self) -> None:
        """Echo until the peer closes."""
        while data := self.request.recv(4096):
            self.request.sendall(data)


@dataclass
class Running:
    """A proxy and an echo "index" on loopback.

    Parameters
    ----------
    port
        The proxy's port.
    index
        The index's port.
    key
        The signing key.
    """

    port: int
    index: int
    key: bytes

    def token(
        self,
        targets: list[tuple[str, int]] | None = None,
        *,
        expires: int = 2**40,
        network: str = "127.0.0.0/8",
        key: bytes | None = None,
    ) -> str:
        """Mint a token, by default for the index on localhost.

        Parameters
        ----------
        targets
            Host and port pairs; the index on ``localhost`` when left out.
        expires
            Epoch seconds.
        network
            The client network it is bound to.
        key
            Another signing key, for a forged token.

        Returns
        -------
        str
            The token.
        """
        return mint(
            key or self.key,
            targets if targets is not None else [("localhost", self.index)],
            expires,
            purpose="build",
            network=ipaddress.ip_network(network),
        )


def _serve(server: socketserver.BaseServer) -> None:
    """Serve on a daemon thread.

    Parameters
    ----------
    server
        The server.
    """
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()


@pytest.fixture
def index() -> Iterator[int]:
    """Run an echo "index" on loopback.

    Yields
    ------
    int
        Its port.
    """
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
    server.daemon_threads = True
    _serve(server)
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _proxy(tmp_path: Path, **changes: object) -> EgressServer:
    """Start a proxy on loopback with a key under `tmp_path`.

    Parameters
    ----------
    tmp_path
        For the key.
    **changes
        Settings to replace.

    Returns
    -------
    EgressServer
        The running proxy.
    """
    ensure_key(tmp_path / "egress.key")
    values: dict[str, object] = {
        "key": tmp_path / "egress.key",
        "listen": ("127.0.0.1", 0),
        "reachable": frozenset({LOOPBACK}),
    }
    values.update(changes)
    server = EgressServer(EgressSettings(**values))  # ty: ignore[invalid-argument-type]
    _serve(server)
    return server


@pytest.fixture
def proxy(tmp_path: Path, index: int) -> Iterator[Running]:
    """Run the proxy, which may reach loopback (as a test needs), and an index.

    Parameters
    ----------
    tmp_path
        For the key.
    index
        The index's port.

    Yields
    ------
    Running
        The proxy, the index and the key.
    """
    server = _proxy(tmp_path)
    yield Running(server.server_address[1], index, ensure_key(tmp_path / "egress.key"))
    server.shutdown()
    server.server_close()


def _head(connection: socket.socket) -> bytes:
    """Read an answer's head.

    Parameters
    ----------
    connection
        The connection.

    Returns
    -------
    bytes
        Everything up to the empty line; what arrived before the connection closed otherwise.
    """
    head = b""
    while b"\r\n\r\n" not in head:
        byte = connection.recv(1)
        if not byte:
            break
        head += byte
    return head


def _connect(
    port: int, target: str, token: str | None, extra: str = ""
) -> tuple[bytes, socket.socket]:
    """Send a CONNECT and read the proxy's answer head.

    Parameters
    ----------
    port
        The proxy.
    target
        ``host:port``.
    token
        The token, or `None` for no credential.
    extra
        More header lines, each ending in CRLF.

    Returns
    -------
    tuple
        The status line and the open connection.
    """
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    auth = ""
    if token is not None:
        credential = base64.b64encode(f"lakana:{token}".encode()).decode()
        auth = f"Proxy-Authorization: Basic {credential}\r\n"
    connection.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}{extra}\r\n".encode())
    return _head(connection).split(b"\r\n", 1)[0], connection


def _status(port: int, target: str, token: str | None, extra: str = "") -> bytes:
    """Send a CONNECT and return only the status code.

    Parameters
    ----------
    port
        The proxy.
    target
        ``host:port``.
    token
        The token, or `None`.
    extra
        More header lines.

    Returns
    -------
    bytes
        The code, such as ``b"403"``.
    """
    answer, connection = _connect(port, target, token, extra)
    connection.close()
    return answer.split()[1]


def test_a_token_for_the_host_and_port_opens_a_tunnel(proxy: Running) -> None:
    """A client with ``localhost:<index>`` in its token reaches it, bytes both ways."""
    status, connection = _connect(proxy.port, f"localhost:{proxy.index}", proxy.token())

    with connection:
        assert status == b"HTTP/1.1 200 Connection established"
        connection.sendall(b"ping")
        assert connection.recv(4) == b"ping"


def test_bytes_sent_right_after_the_head_reach_the_index(proxy: Running) -> None:
    """A client that does not wait for the 200 loses nothing."""
    connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    credential = base64.b64encode(f"lakana:{proxy.token()}".encode()).decode()
    connection.sendall(
        f"CONNECT localhost:{proxy.index} HTTP/1.1\r\nProxy-Authorization: Basic {credential}"
        "\r\n\r\nearly".encode()
    )
    with connection:
        assert _head(connection).startswith(b"HTTP/1.1 200 ")
        assert connection.recv(5) == b"early"


@pytest.mark.parametrize(
    ("target", "targets", "network", "status"),
    [
        ("example.org:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"403"),
        ("localhost:1", [("localhost", "{port}")], "127.0.0.0/8", b"403"),
        ("localhost:{port}", [("localhost", 443)], "127.0.0.0/8", b"403"),
        ("localhost:{port}", [], "127.0.0.0/8", b"403"),
        ("localhost:{port}", [("localhost", "{port}")], "10.207.0.8/29", b"403"),
        ("LOCALHOST:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"200"),
        ("localhost.:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("localhost:0{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("localhost:+{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("localhost", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("[::1]:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("local\x1bhost:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("user@localhost:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
    ],
    ids=[
        "host-not-in-token",
        "port-not-in-token",
        "host-with-another-port",
        "no-targets",
        "token-of-another-network",
        "case-insensitive-host",
        "trailing-dot",
        "leading-zero-port",
        "signed-port",
        "no-port",
        "ip-v6-literal",
        "control-character",
        "userinfo",
    ],
)
def test_everything_outside_the_token_is_refused(
    proxy: Running, target: str, targets: list[tuple[str, object]], network: str, status: bytes
) -> None:
    """Review focus: exactly the token's host:port pairs, from the token's own network.

    Parameters
    ----------
    proxy
        The running proxy.
    target
        The CONNECT target.
    targets
        The token's pairs; ``{port}`` is the index's port.
    network
        The client network the token is bound to.
    status
        The expected status code.
    """
    pairs = [(h, int(str(p).format(port=proxy.index))) for h, p in targets]
    token = proxy.token(pairs, network=network)

    assert _status(proxy.port, target.format(port=proxy.index), token) == status


@pytest.mark.parametrize(
    "credential",
    [
        None,
        "forged",
        "expired",
        "Bearer {token}",
        "Basic !!!",
        "Basic {b64}",
    ],
)
def test_a_missing_or_bad_credential_is_asked_for(proxy: Running, credential: str | None) -> None:
    """407 with a challenge, whatever is wrong with the credential.

    Parameters
    ----------
    proxy
        The running proxy.
    credential
        What goes in the header.
    """
    target = f"localhost:{proxy.index}"
    if credential == "forged":
        status = _status(proxy.port, target, proxy.token(key=b"z" * 32))
    elif credential == "expired":
        status = _status(proxy.port, target, proxy.token(expires=int(time.time()) - 1))
    elif credential is None:
        status = _status(proxy.port, target, None)
    else:
        value = credential.format(
            token=proxy.token(), b64=base64.b64encode(proxy.token().encode()).decode()
        )
        status = _status(proxy.port, target, None, f"Proxy-Authorization: {value}\r\n")
    assert status == b"407"


def test_anything_but_connect_is_refused(proxy: Running) -> None:
    """A plain GET through the proxy is not a tunnel to an https index."""
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as plain:
        plain.sendall(b"GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert _head(plain).split()[1] == b"405"


@pytest.mark.parametrize(
    "head",
    [
        "CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {c}\r\n"
        "Proxy-Authorization: Basic {c}\r\n\r\n",
        "CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {c}\r\n folded\r\n\r\n",
        "CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {c}\nX: y\r\n\r\n",
        "CONNECT {target} HTTP/1.1\r\nno colon\r\nProxy-Authorization: Basic {c}\r\n\r\n",
        "CONNECT {target} HTTP/1.1\r\n"
        + "X-A: b\r\n" * 70
        + "Proxy-Authorization: Basic {c}\r\n\r\n",
        "CONNECT {target} HTTP/1.1\r\nX-A: " + "b" * 20000 + "\r\n\r\n",
        "CONNECT {target} HTTP/1.1 extra\r\nProxy-Authorization: Basic {c}\r\n\r\n",
        "CONNECT {target} SPDY/3\r\nProxy-Authorization: Basic {c}\r\n\r\n",
    ],
    ids=[
        "duplicate-credential",
        "folded-line",
        "bare-line-feed",
        "line-without-colon",
        "too-many-headers",
        "too-long",
        "four-words",
        "not-http",
    ],
)
def test_an_ambiguous_head_is_refused(proxy: Running, head: str) -> None:
    """Nothing the proxy and a client could read in two ways gets a tunnel.

    Parameters
    ----------
    proxy
        The running proxy.
    head
        The request head.
    """
    credential = base64.b64encode(f"lakana:{proxy.token()}".encode()).decode()
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as connection:
        connection.sendall(head.format(target=f"localhost:{proxy.index}", c=credential).encode())
        assert _head(connection).split()[1] == b"400"


def test_a_slow_head_is_cut_off(tmp_path: Path) -> None:
    """A client that drips its head byte by byte gets 408 at the deadline, not per byte."""
    server = _proxy(tmp_path, head_seconds=1.0)
    try:
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as slow:
            started = time.monotonic()
            for byte in b"CONNECT localhost:1 HTTP/1.1\r\nX-Drip: " + b"a" * 100:
                if select.select([slow], [], [], 0.2)[0] or time.monotonic() - started > 4:
                    break
                slow.sendall(bytes([byte]))
            answer = _head(slow)
            elapsed = time.monotonic() - started
        assert answer.split()[1] == b"408"
        assert elapsed < 3
    finally:
        server.shutdown()
        server.server_close()


def test_a_tunnel_ends_when_its_token_expires(proxy: Running) -> None:
    """A token's time bounds the tunnels it opened, not only their start."""
    status, connection = _connect(
        proxy.port, f"localhost:{proxy.index}", proxy.token(expires=int(time.time()) + 2)
    )
    with connection:
        assert status.split()[1] == b"200"
        connection.settimeout(6)
        assert connection.recv(1) == b""


def test_a_full_proxy_says_it_is_busy(tmp_path: Path, index: int) -> None:
    """Past `max_connections`, a client gets 503 at once instead of a hanging connection."""
    server = _proxy(tmp_path, max_connections=1)
    key = ensure_key(tmp_path / "egress.key")
    running = Running(server.server_address[1], index, key)
    try:
        status, first = _connect(running.port, f"localhost:{index}", running.token())
        with first:
            assert status.split()[1] == b"200"
            assert _status(running.port, f"localhost:{index}", running.token()) == b"503"
    finally:
        server.shutdown()
        server.server_close()


def test_a_flood_beyond_the_spare_threads_is_closed_without_a_thread(tmp_path: Path) -> None:
    """When every thread is taken, a new connection is closed at once."""
    server = _proxy(tmp_path, max_connections=1)
    taken = 0
    try:
        while server.threads.acquire(blocking=False):
            taken += 1
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as late:
            assert late.recv(1) == b""
    finally:
        for _ in range(taken):
            server.threads.release()
        server.shutdown()
        server.server_close()

    assert taken == 65


@pytest.mark.parametrize(
    ("host", "reachable"),
    [
        ("localhost", frozenset()),
        ("127.0.0.1", frozenset()),
        ("169.254.169.254", frozenset({LOOPBACK})),
        ("0.0.0.0", frozenset({LOOPBACK})),
        ("100.100.100.200", frozenset({LOOPBACK})),
        ("224.0.0.1", frozenset({LOOPBACK})),
    ],
    ids=[
        "loopback-name",
        "loopback-literal",
        "metadata",
        "unspecified",
        "alibaba-metadata",
        "multicast",
    ],
)
def test_a_signed_host_on_a_forbidden_address_is_refused(
    tmp_path: Path, index: int, host: str, reachable: frozenset[object]
) -> None:
    """Loopback, link-local (cloud metadata), multicast: never, whatever the token says.

    Parameters
    ----------
    tmp_path
        For the key.
    index
        The index's port.
    host
        The signed host.
    reachable
        Networks the test lets the proxy reach.
    """
    server = _proxy(tmp_path, reachable=reachable)
    running = Running(server.server_address[1], index, ensure_key(tmp_path / "egress.key"))
    try:
        answer, connection = _connect(
            running.port, f"{host}:{index}", running.token([(host, index)])
        )
        body = connection.recv(4096)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()

    assert answer.split()[1] == b"403"
    assert b"not reachable" in body


def test_a_connected_network_is_read_from_the_routing_table(tmp_path: Path) -> None:
    """The networks the proxy's container is attached to: its Compose network and every build's."""
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "eth1\t0800CF0A\t00000000\t0001\t0\t0\t0\tF8FFFFFF\t0\t0\t0\n",
        encoding="ascii",
    )
    route6 = tmp_path / "ipv6_route"
    route6.write_text(
        "fd000000000000000000000000000000 40 00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00000100 00000001 00000000 00000001 eth0\n"
        "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
        "fd000000000000000000000000000001 00000400 00000001 00000000 00000003 eth0\n",
        encoding="ascii",
    )

    found = connected_networks(route, route6)

    assert found == frozenset(
        {
            ipaddress.ip_network("172.18.0.0/16"),
            ipaddress.ip_network("10.207.0.8/29"),
            ipaddress.ip_network("fd00::/64"),
        }
    )
    assert connected_networks(tmp_path / "missing", route6) is None
    assert connected_networks(route, tmp_path / "missing6") == frozenset(
        {ipaddress.ip_network("172.18.0.0/16"), ipaddress.ip_network("10.207.0.8/29")}
    )


_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
_GOOD = "eth1\t0800CF0A\t00000000\t0001\t0\t0\t0\tF8FFFFFF\n"
_GOOD6 = (
    "fd000000000000000000000000000000 40 00000000000000000000000000000000 00 "
    "00000000000000000000000000000000 00000100 00000001 00000000 00000001 eth0\n"
)


@pytest.mark.parametrize(
    ("route", "route6"),
    [
        (_HEADER + "eth0\tZZZZ\t00000000\t0001\t0\t0\t0\t0000FFFF\n" + _GOOD, _GOOD6),
        (_HEADER + "short\n" + _GOOD, _GOOD6),
        (_HEADER + _GOOD, "zz 40 x x 00 x x x x eth0\n"),
        (_HEADER + _GOOD, "fd00 40\n"),
    ],
    ids=["bad-ipv4-field", "short-ipv4-line", "bad-ipv6-field", "short-ipv6-line"],
)
def test_one_route_line_not_understood_fails_closed(
    tmp_path: Path, route: str, route6: str
) -> None:
    """A line the parser cannot read might be the attached network it would have refused.

    Parameters
    ----------
    tmp_path
        For the tables.
    route
        The IPv4 table.
    route6
        The IPv6 table.
    """
    (tmp_path / "route").write_text(route, encoding="ascii")
    (tmp_path / "ipv6_route").write_text(route6, encoding="ascii")

    assert connected_networks(tmp_path / "route", tmp_path / "ipv6_route") is None
    (tmp_path / "ipv6_route").write_text(_GOOD6 + "\n", encoding="ascii")
    (tmp_path / "route").write_text(_HEADER + _GOOD + "\n", encoding="ascii")
    assert len(connected_networks(tmp_path / "route", tmp_path / "ipv6_route") or ()) == 2


def test_an_unreadable_ipv6_table_fails_closed_and_a_missing_one_means_ipv6_is_off(
    tmp_path: Path,
) -> None:
    """Only a table that does not exist is IPv6 switched off; any other error refuses."""
    (tmp_path / "route").write_text(_HEADER + _GOOD, encoding="ascii")
    (tmp_path / "ipv6_route").mkdir()

    assert connected_networks(tmp_path / "route", tmp_path / "ipv6_route") is None
    assert connected_networks(tmp_path / "route", tmp_path / "missing") == frozenset(
        {ipaddress.ip_network("10.207.0.8/29")}
    )


def test_without_a_routing_table_no_direct_connection_is_made(
    proxy: Running, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed: without its attached networks the proxy cannot tell internal from outside.

    Parameters
    ----------
    proxy
        The running proxy (its index would otherwise be reachable).
    tmp_path
        Where no table is.
    caplog
        The log.
    """
    server = _proxy(tmp_path, route=tmp_path / "missing")
    try:
        answer, connection = _connect(
            server.server_address[1], f"localhost:{proxy.index}", proxy.token()
        )
        body = connection.recv(4096)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()

    assert answer.split()[1] == b"503"
    assert b"routing table" in body
    assert "missing" in caplog.text


def test_an_upstream_proxy_is_asked_to_connect(tmp_path: Path) -> None:
    """`LAKANA_EGRESS__UPSTREAM_PROXY`: the company proxy gets the CONNECT, with its credential."""
    seen: list[bytes] = []

    class Upstream(socketserver.BaseRequestHandler):
        """Records the CONNECT and accepts it."""

        def handle(self) -> None:
            """Read the head, answer 200, echo."""
            seen.append(_head(self.request))
            self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
            while data := self.request.recv(4096):
                self.request.sendall(data)

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    _serve(upstream)
    server = _proxy(
        tmp_path,
        reachable=frozenset(),
        upstream=f"http://corp:p%40ss@127.0.0.1:{upstream.server_address[1]}",
    )
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("pypi.org", 443)], 2**40, purpose="build", network=LOOPBACK)
        status, connection = _connect(server.server_address[1], "pypi.org:443", token)
        with connection:
            connection.sendall(b"ping")
            echoed = connection.recv(4)
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()

    assert status == b"HTTP/1.1 200 Connection established"
    assert echoed == b"ping"
    assert seen[0].startswith(b"CONNECT pypi.org:443 HTTP/1.1\r\n")
    assert b"Proxy-Authorization: Basic " + base64.b64encode(b"corp:p@ss") in seen[0]
    assert b"lakana" not in seen[0]


def test_an_upstream_refusal_is_a_bad_gateway(tmp_path: Path) -> None:
    """The upstream's answer is named; its credential is not."""

    class Refusing(socketserver.BaseRequestHandler):
        """Refuses every CONNECT."""

        def handle(self) -> None:
            """Read the head, answer 403."""
            _head(self.request)
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Refusing)
    upstream.daemon_threads = True
    _serve(upstream)
    server = _proxy(tmp_path, upstream=f"http://corp:secret@127.0.0.1:{upstream.server_address[1]}")
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("pypi.org", 443)], 2**40, purpose="build", network=LOOPBACK)
        answer, connection = _connect(server.server_address[1], "pypi.org:443", token)
        body = connection.recv(4096)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()

    assert answer.split()[1] == b"502"
    assert b"403 Forbidden" in body
    assert b"secret" not in body
    assert "secret" not in repr(server.settings)


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"LAKANA_EGRESS__UPSTREAM_PROXY": "socks5://proxy:1080"}, "LAKANA_EGRESS__UPSTREAM_PROXY"),
        ({"LAKANA_EGRESS__UPSTREAM_PROXY": "http://proxy:99999"}, "LAKANA_EGRESS__UPSTREAM_PROXY"),
        (
            {"LAKANA_EGRESS__UPSTREAM_PROXY": "http://proxy:3128/path?q"},
            "LAKANA_EGRESS__UPSTREAM_PROXY",
        ),
        ({"LAKANA_EGRESS__LISTEN": "3128"}, "LAKANA_EGRESS__LISTEN"),
        ({"LAKANA_EGRESS__LISTEN": "0.0.0.0:70000"}, "LAKANA_EGRESS__LISTEN"),
        ({"LAKANA_EGRESS__MAX_CONNECTIONS": "0"}, "LAKANA_EGRESS__MAX_CONNECTIONS"),
        ({"LAKANA_EGRESS__PORTS": "443"}, "LAKANA_EGRESS__PORTS"),
    ],
)
def test_a_bad_setting_names_itself(environ: dict[str, str], message: str) -> None:
    """Each message names the setting; an unknown one is refused, not ignored.

    Parameters
    ----------
    environ
        The proxy's environment.
    message
        Part of the error.
    """
    with pytest.raises(SettingsError, match=message):
        load_settings(environ)


def test_the_defaults_work_and_the_upstream_credential_stays_out_of_repr() -> None:
    """Without settings: port 3128 on every address, the key in the shared volume."""
    settings = load_settings({"LAKANA_EGRESS__UPSTREAM_PROXY": "http://u:pw@proxy:8080"})

    assert settings.listen == ("0.0.0.0", 3128)  # nosec B104
    assert settings.key == Path("/run/lakana-egress/egress.key")
    assert settings.upstream == "http://u:pw@proxy:8080"
    assert "pw" not in repr(settings)
    assert settings.reachable == frozenset()


def test_an_unreachable_destination_is_a_bad_gateway(proxy: Running) -> None:
    """A signed host that refuses the connection, or does not resolve, gets 502 with the cause."""
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()

    assert _status(proxy.port, f"localhost:{port}", proxy.token([("localhost", port)])) == b"502"
    missing = "no-such-host.invalid"
    assert _status(proxy.port, f"{missing}:443", proxy.token([(missing, 443)])) == b"502"


def test_an_unreachable_upstream_is_a_bad_gateway(tmp_path: Path) -> None:
    """The message says the upstream proxy cannot be reached."""
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    server = _proxy(tmp_path, upstream=f"http://127.0.0.1:{port}")
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("pypi.org", 443)], 2**40, purpose="build", network=LOOPBACK)
        answer, connection = _connect(server.server_address[1], "pypi.org:443", token)
        body = connection.recv(4096)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()

    assert answer.split()[1] == b"502"
    assert b"cannot reach the upstream proxy" in body


def test_a_new_key_is_picked_up_without_a_restart(proxy: Running, tmp_path: Path) -> None:
    """A launcher start replaces the key; at its next connection the proxy reads the new one."""
    old = proxy.token()
    assert _status(proxy.port, f"localhost:{proxy.index}", old) == b"200"
    fresh = rotate_key(tmp_path / "egress.key")  # what a launcher start does

    assert _status(proxy.port, f"localhost:{proxy.index}", old) == b"407"
    new = mint(fresh, [("localhost", proxy.index)], 2**40, purpose="build", network=LOOPBACK)
    assert _status(proxy.port, f"localhost:{proxy.index}", new) == b"200"


def test_without_a_key_every_client_is_asked_for_a_token(proxy: Running, tmp_path: Path) -> None:
    """Until the launcher writes the key, nothing passes."""
    token = proxy.token()
    (tmp_path / "egress.key").unlink()

    assert _status(proxy.port, f"localhost:{proxy.index}", token) == b"407"


def test_a_failed_key_read_is_tried_again_at_the_next_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient read error must not deny every client until the next launcher start.

    Parameters
    ----------
    tmp_path
        For the key.
    monkeypatch
        Makes the first read fail.
    """
    key_file = tmp_path / "egress.key"
    key = ensure_key(key_file)
    settings = EgressSettings(key=key_file, listen=("127.0.0.1", 0))
    server = EgressServer(settings)
    try:
        real = egress.read_key
        monkeypatch.setattr(egress, "read_key", lambda path: None)
        assert server.key() is None
        monkeypatch.setattr(egress, "read_key", real)

        assert server.key() == key  # the file did not change, but nothing was cached
    finally:
        server.server_close()


def test_an_ipv4_mapped_address_is_judged_as_its_ipv4_address() -> None:
    """``::ffff:127.0.0.1`` is loopback."""
    mapped = ipaddress.ip_address("::ffff:127.0.0.1")

    assert egress.forbidden(mapped, frozenset(), frozenset())
    assert not egress.forbidden(mapped, frozenset(), frozenset({LOOPBACK}))
    assert not egress.forbidden(ipaddress.ip_address("10.1.2.3"), frozenset(), frozenset())


def test_a_listen_address_is_read(tmp_path: Path) -> None:
    """`LAKANA_EGRESS__LISTEN` and `LAKANA_EGRESS__KEY` take effect."""
    settings = load_settings(
        {"LAKANA_EGRESS__LISTEN": "127.0.0.1:3129", "LAKANA_EGRESS__KEY": str(tmp_path / "k")}
    )

    assert (settings.listen, settings.key) == (("127.0.0.1", 3129), tmp_path / "k")


def test_main_stops_with_the_setting_named(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad setting ends the process with status 2 and the reason in the log.

    Parameters
    ----------
    monkeypatch
        Sets the environment.
    caplog
        The log.
    """
    monkeypatch.setenv("LAKANA_EGRESS__MAX_CONNECTIONS", "lots")

    with pytest.raises(SystemExit) as stopped:
        egress.main()

    assert stopped.value.code == 2
    assert "LAKANA_EGRESS__MAX_CONNECTIONS" in caplog.text


def test_main_serves_and_warns_without_a_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """At start the log says when the launcher has not written the key yet.

    Parameters
    ----------
    monkeypatch
        Sets the environment and stops the serving loop.
    caplog
        The log.
    tmp_path
        Where no key is.
    """
    monkeypatch.setenv("LAKANA_EGRESS__KEY", str(tmp_path / "none"))
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    monkeypatch.setenv("LAKANA_EGRESS__LISTEN", f"127.0.0.1:{free.getsockname()[1]}")
    free.close()
    served: list[EgressServer] = []

    def once(self: EgressServer, poll_interval: float = 0.5) -> None:
        """Record the server instead of serving.

        Parameters
        ----------
        self
            The server.
        poll_interval
            Unused.
        """
        served.append(self)

    monkeypatch.setattr(EgressServer, "serve_forever", once)

    egress.main()
    served[0].server_close()

    assert "No key at" in caplog.text
