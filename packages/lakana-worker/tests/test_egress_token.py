"""The egress token: a purpose, a client network, host:port pairs and an expiry, signed."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from lakana_worker.launcher.egress_token import Grant, ensure_key, mint, read_key, verify

KEY = b"k" * 32
NETWORK = ipaddress.ip_network("10.207.0.8/29")


def _token(**changes: object) -> str:
    """Mint a token for pypi.org until 2000, with fields replaced.

    Parameters
    ----------
    **changes
        Keyword arguments of `mint` to replace.

    Returns
    -------
    str
        The token.
    """
    values: dict[str, object] = {
        "targets": [("pypi.org", 443), ("files.pythonhosted.org", 443)],
        "expires": 2000,
        "purpose": "build",
        "network": NETWORK,
    }
    values.update(changes)
    return mint(KEY, **values)  # type: ignore[arg-type]


def test_a_token_carries_its_grant_until_it_expires() -> None:
    """The proxy needs no state: the token says everything, and the signature vouches for it."""
    token = _token()

    assert verify(KEY, token, now=2000) == Grant(
        purpose="build",
        network=NETWORK,
        targets=frozenset({("pypi.org", 443), ("files.pythonhosted.org", 443)}),
        expires=2000,
    )
    assert verify(KEY, token, now=2001) is None
    assert verify(b"x" * 32, token, now=1999) is None


def test_a_token_is_safe_in_the_userinfo_of_a_url() -> None:
    """It goes into `HTTPS_PROXY` as the password, unquoted."""
    token = _token()

    assert set(token) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def _swap_payload(token: str) -> str:
    """Keep the signature, sign nothing new, and grant another host.

    Parameters
    ----------
    token
        A valid token.

    Returns
    -------
    str
        The forged token.
    """
    version, payload, signature = token.split(".")
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    data["t"] = ["evil.example.org:443"]
    forged = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{version}.{forged}.{signature}"


@pytest.mark.parametrize(
    "forge",
    [
        lambda t: t.replace("v1.", "v2.", 1),
        _swap_payload,
        lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),
        lambda t: t.upper(),
        lambda t: t.rsplit(".", 1)[0],
        lambda t: t + ".x",
        lambda t: "",
        lambda t: t.replace("v1.", "v1.²", 1),
        lambda t: t[:-1] + "é",
    ],
    ids=[
        "version",
        "payload",
        "signature",
        "case",
        "truncated",
        "extended",
        "empty",
        "unicode-digit",
        "non-ascii",
    ],
)
def test_a_changed_token_is_refused(forge: Callable[[str], str]) -> None:
    """Changing any field breaks the signature; malformed text never raises.

    Parameters
    ----------
    forge
        Turns a valid token into a forged one.
    """
    assert verify(KEY, forge(_token()), now=1000) is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"purpose": "Build!"}, "purpose"),
        ({"targets": [("pypi.org", 0)]}, "pypi.org:0"),
        ({"targets": [("pypi.org", 65536)]}, "pypi.org:65536"),
        ({"targets": [("PyPI.org", 443)]}, "PyPI.org"),
        ({"targets": [("pypi.org,evil.org", 443)]}, "pypi.org,evil.org"),
        ({"targets": [("pypi.org:80", 443)]}, "pypi.org:80"),
        ({"targets": [("pypi.org", 443)] * 65}, "64"),
        ({"expires": -1}, "expires"),
    ],
)
def test_a_token_is_never_minted_for_something_the_proxy_would_misread(
    changes: dict[str, object], message: str
) -> None:
    """The launcher refuses to sign a host or a port the proxy could match in two ways.

    Parameters
    ----------
    changes
        Arguments of `mint` to replace.
    message
        Part of the error.
    """
    with pytest.raises(ValueError, match=message):
        _token(**changes)


def test_the_key_is_made_once_and_readable_by_the_group(tmp_path: Path) -> None:
    """The launcher writes it; the proxy, in the launcher's group, reads it."""
    path = tmp_path / "egress.key"

    first = ensure_key(path)

    assert ensure_key(path) == first == read_key(path)
    assert len(first) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert read_key(tmp_path / "missing") is None
    assert [p.name for p in tmp_path.iterdir()] == ["egress.key"]


def test_a_broken_key_is_replaced_and_a_link_is_never_followed(tmp_path: Path) -> None:
    """A truncated file gets a new key; a symbolic link planted at the path is not read."""
    path = tmp_path / "egress.key"
    path.write_text("abc", encoding="ascii")

    assert read_key(path) is None
    fresh = ensure_key(path)
    assert read_key(path) == fresh

    target = tmp_path / "elsewhere"
    target.write_text(fresh.hex(), encoding="ascii")
    path.unlink()
    os.symlink(target, path)
    assert read_key(path) is None
    replaced = ensure_key(path)
    assert not path.is_symlink()
    assert replaced != fresh
    assert target.read_text(encoding="ascii") == fresh.hex()


def _signed(payload: object) -> str:
    """Sign an arbitrary JSON payload as the launcher would, to test what the proxy accepts.

    Parameters
    ----------
    payload
        Any JSON value, or bytes taken as the payload's text.

    Returns
    -------
    str
        A correctly signed token around it.
    """
    text = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    body = "v1." + base64.urlsafe_b64encode(text).decode().rstrip("=")
    return f"{body}.{hmac.new(KEY, body.encode(), hashlib.sha256).hexdigest()}"


GOOD = {"e": 2000, "n": "10.207.0.8/29", "p": "build", "t": ["pypi.org:443"]}


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"\xff\xfe",
        ["a list"],
        {**GOOD, "x": 1},
        {**GOOD, "e": "2000"},
        {**GOOD, "e": True},
        {**GOOD, "e": 2000.0},
        {**GOOD, "p": "Build"},
        {**GOOD, "p": 7},
        {**GOOD, "t": "pypi.org:443"},
        {**GOOD, "t": ["pypi.org:443"] * 65},
        {**GOOD, "t": [443]},
        {**GOOD, "t": ["pypi.org:0443"]},
        {**GOOD, "t": ["pypi.org:65536"]},
        {**GOOD, "t": ["pypi.org:²"]},
        {**GOOD, "t": ["PyPI.org:443"]},
        {**GOOD, "n": 7},
        {**GOOD, "n": "10.207.0.9/29"},
        {**GOOD, "n": "nowhere"},
    ],
)
def test_a_signed_but_malformed_payload_is_refused(payload: object) -> None:
    """Even with the right key, the proxy accepts only what `mint` writes.

    Parameters
    ----------
    payload
        The payload.
    """
    assert verify(KEY, _signed(GOOD), now=1000) is not None
    assert verify(KEY, _signed(payload), now=1000) is None


def test_two_launchers_racing_agree_on_one_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second to link finds the first's key and uses it.

    Parameters
    ----------
    tmp_path
        For the key.
    monkeypatch
        Lets the other launcher win between the check and the link.
    """
    path = tmp_path / "egress.key"
    real_link = os.link

    def first_wins(source: Path, target: Path) -> None:
        """Write the other launcher's key just before this one links.

        Parameters
        ----------
        source
            The temporary file.
        target
            The key path.
        """
        path.write_text("ab" * 32, encoding="ascii")
        real_link(source, target)

    monkeypatch.setattr(os, "link", first_wins)

    assert ensure_key(path) == bytes.fromhex("ab" * 32)
    assert [p.name for p in tmp_path.iterdir()] == ["egress.key"]


def test_a_directory_at_the_key_path_is_no_key(tmp_path: Path) -> None:
    """Something other than a regular file is not read."""
    (tmp_path / "egress.key").mkdir()

    assert read_key(tmp_path / "egress.key") is None


def test_a_token_too_long_for_the_proxy_is_never_minted() -> None:
    """`mint` refuses what `verify` would always refuse, instead of a token that never works."""
    hosts = [("a" * 60 + "." + "b" * 60 + "." + f"h{n}.example.org", 443) for n in range(49)]

    with pytest.raises(ValueError, match="too long"):
        _token(targets=hosts)
    assert verify(KEY, _token(targets=hosts[:20]), now=1000) is not None


def test_an_existing_key_gets_its_mode_back(tmp_path: Path) -> None:
    """A key someone opened up to everyone is closed again, and kept."""
    path = tmp_path / "egress.key"
    first = ensure_key(path)
    path.chmod(0o644)

    assert ensure_key(path) == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
