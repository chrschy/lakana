"""The launcher refuses every request shape it does not run, and fixes every field that matters.

The hostile shapes are the trial's sixteen and more: each one would, through a socket proxy,
have reached Docker as a host bind, a privileged container, the host's PID namespace, a device,
an added capability or the host network.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from lakana_worker.launcher.requests import (
    LABEL_OWNER,
    LABEL_STACK,
    MAX_REQUEST_BYTES,
    BuildRequest,
    CleanupRequest,
    Refused,
    RunRequest,
    StatusRequest,
    StopRequest,
    build_container_spec,
    cache_volume,
    environment_volume,
    network_spec,
    parse_request,
    probe_container_spec,
    run_container_spec,
    uv_index_value,
)
from lakana_worker.launcher.settings import LauncherSettings

TOKEN = "t" * 64
KEY = "a" * 64
SETTINGS = LauncherSettings(
    stack="lakana",
    images={"3.13": "ghcr.io/chrschy/lakana-task:0.14.0-py3.13"},
    work_volume="lakana-task-work",
    socket=Path("/run/lakana-launcher/launcher.sock"),
    ingestion_roots={"landing": "/srv/landing"},
)
RUN: dict[str, Any] = {
    "token": TOKEN,
    "op": "run",
    "task": "0f3c9a1e",
    "owner": "worker-1",
    "python": "3.13",
    "environment": KEY,
    "attempt": "attempt-abc_12",
    "memory_mb": 2048,
    "cpus": 1,
    "pids": 256,
    "timeout_seconds": 600,
    "kill_grace_seconds": 10,
    "stdin": '{"version": 2}',
    "roots": [],
}
BUILD: dict[str, Any] = {
    "token": TOKEN,
    "op": "build",
    "build": "b0f3c9a1e",
    "owner": "worker-1",
    "python": "3.13",
    "environment": KEY,
    "attempt": "attempt-abc_12",
    "indexes": [
        {"url": "https://pkgs.example.org/simple", "username": "ci", "password": "p@ss word"}
    ],
    "operator_index": None,
    "hosts": ["pkgs.example.org", "pypi.org", "files.pythonhosted.org"],
    "no_build": True,
    "memory_mb": 2048,
    "cpus": 1,
    "pids": 256,
    "timeout_seconds": 600,
    "max_bytes": 5 * 1024**3,
}


def _raw(request: object) -> bytes:
    """Encode a request as the client sends it.

    Parameters
    ----------
    request
        Any JSON value.

    Returns
    -------
    bytes
        One line.
    """
    return json.dumps(request).encode() + b"\n"


def _with(base: dict[str, Any], **changes: object) -> dict[str, Any]:
    """Copy a request with some fields changed.

    Parameters
    ----------
    base
        The valid request.
    **changes
        Fields to set.

    Returns
    -------
    dict
        The changed copy.
    """
    request = copy.deepcopy(base)
    request.update(changes)
    return request


def _without(base: dict[str, Any], key: str) -> dict[str, Any]:
    """Copy a request without one field.

    Parameters
    ----------
    base
        The valid request.
    key
        The field to drop.

    Returns
    -------
    dict
        The copy.
    """
    request = copy.deepcopy(base)
    del request[key]
    return request


HOSTILE: list[Any] = [
    pytest.param(
        _with(RUN, HostConfig={"Privileged": True}), "run takes exactly", id="extra-hostconfig"
    ),
    pytest.param(_with(RUN, binds=["/:/host"]), "run takes exactly", id="extra-binds"),
    pytest.param(_with(RUN, privileged=True), "run takes exactly", id="extra-privileged"),
    pytest.param(_without(RUN, "roots"), "run takes exactly", id="missing-field"),
    pytest.param(_with(RUN, python="3.10"), "python must be one of 3.13", id="image-not-allowed"),
    pytest.param(
        _with(RUN, python="python:3.13-slim"), "python must be one of", id="image-reference"
    ),
    pytest.param(_with(RUN, environment="../../../etc"), "environment:", id="key-traversal"),
    pytest.param(_with(RUN, environment="A" * 64), "environment:", id="key-upper-case"),
    pytest.param(_with(RUN, attempt="../attempt-x"), "attempt:", id="attempt-traversal"),
    pytest.param(_with(RUN, attempt="attempt-x/../.."), "attempt:", id="attempt-inner-dots"),
    pytest.param(_with(RUN, attempt="attempt-"), "attempt:", id="attempt-empty"),
    pytest.param(_with(RUN, task="x&HostConfig.Privileged=1"), "task:", id="task-query"),
    pytest.param(_with(RUN, task="../x"), "task:", id="task-traversal"),
    pytest.param(_with(RUN, task="X"), "task:", id="task-upper-case"),
    pytest.param(_with(RUN, task="a" * 64), "task:", id="task-too-long"),
    pytest.param(_with(RUN, owner="worker 1"), "owner:", id="owner-space"),
    pytest.param(_with(RUN, owner="w/1"), "owner:", id="owner-slash"),
    pytest.param(
        _with(RUN, memory_mb=100_000), "memory_mb: an integer from 64 to", id="memory-ceiling"
    ),
    pytest.param(_with(RUN, memory_mb=True), "memory_mb:", id="memory-bool"),
    pytest.param(_with(RUN, memory_mb=1.5), "memory_mb:", id="memory-float"),
    pytest.param(_with(RUN, memory_mb=0), "memory_mb:", id="memory-zero"),
    pytest.param(_with(RUN, cpus="2"), "cpus:", id="cpus-text"),
    pytest.param(_with(RUN, cpus=0), "cpus:", id="cpus-zero"),
    pytest.param(_with(RUN, cpus=65), "cpus:", id="cpus-ceiling"),
    pytest.param(_with(RUN, pids=0), "pids:", id="pids-unlimited"),
    pytest.param(_with(RUN, pids=-1), "pids:", id="pids-negative"),
    pytest.param(_with(RUN, timeout_seconds=0), "timeout_seconds:", id="timeout-zero"),
    pytest.param(_with(RUN, kill_grace_seconds=301), "kill_grace_seconds:", id="grace-ceiling"),
    pytest.param(_with(RUN, stdin="x" * (256 * 1024 + 1)), "stdin:", id="stdin-too-long"),
    pytest.param(_with(RUN, stdin={"a": 1}), "stdin:", id="stdin-not-text"),
    pytest.param(_with(RUN, roots=["/"]), "roots:", id="root-a-path"),
    pytest.param(_with(RUN, roots=["landing", "landing"]), "roots:", id="root-twice"),
    pytest.param(_with(RUN, roots=[["landing"]]), "roots:", id="root-not-text"),
    pytest.param(_with(RUN, roots="landing"), "roots:", id="roots-not-a-list"),
    pytest.param(_with(RUN, pid="host"), "run takes exactly", id="extra-pid-host"),
    pytest.param(_with(RUN, PidMode="host"), "run takes exactly", id="extra-pidmode-host"),
    pytest.param(_with(RUN, devices=["/dev/sda"]), "run takes exactly", id="extra-device"),
    pytest.param(
        _with(RUN, Devices=[{"PathOnHost": "/dev/mem"}]), "run takes exactly", id="extra-Devices"
    ),
    pytest.param(_with(RUN, cap_add=["SYS_ADMIN"]), "run takes exactly", id="extra-cap-add"),
    pytest.param(_with(RUN, CapAdd=["ALL"]), "run takes exactly", id="extra-CapAdd"),
    pytest.param(_with(RUN, network="host"), "run takes exactly", id="extra-network-host"),
    pytest.param(
        _with(RUN, network_mode="host"), "run takes exactly", id="extra-network-mode-host"
    ),
    pytest.param(_with(RUN, image="alpine"), "run takes exactly", id="extra-image"),
    pytest.param(_with(BUILD, pid="host"), "build takes exactly", id="build-extra-pid-host"),
    pytest.param(_with(BUILD, binds=["/:/host"]), "build takes exactly", id="build-extra-binds"),
    pytest.param(
        _with(BUILD, cap_add=["SYS_ADMIN"]), "build takes exactly", id="build-extra-cap-add"
    ),
    pytest.param(
        _with(BUILD, network="host"), "build takes exactly", id="build-extra-network-host"
    ),
    pytest.param(_without(BUILD, "hosts"), "build takes exactly", id="build-missing-field"),
    pytest.param(
        {"op": "status", "token": TOKEN, "python": "3.13", "environment": KEY, "pid": "host"},
        "status takes exactly",
        id="status-extra",
    ),
    pytest.param(
        {"op": "status", "token": TOKEN, "python": "3.13"},
        "status takes exactly",
        id="status-missing",
    ),
    pytest.param(
        {"op": "status", "token": TOKEN, "python": "3.10", "environment": KEY},
        "python must be one of",
        id="status-python",
    ),
    pytest.param(
        {"op": "cleanup", "token": TOKEN, "owner": "w", "all": True},
        "cleanup takes exactly",
        id="cleanup-extra",
    ),
    pytest.param({"op": "cleanup", "token": TOKEN}, "cleanup takes exactly", id="cleanup-missing"),
    pytest.param(
        {"op": "cleanup", "token": TOKEN, "owner": "w; rm -rf /"},
        "owner:",
        id="cleanup-owner-shell",
    ),
    pytest.param({"op": "stop", "token": TOKEN}, "stop takes exactly", id="stop-missing"),
    pytest.param(
        _with(RUN, Mounts=[{"Type": "bind", "Source": "/"}]), "run takes exactly", id="extra-Mounts"
    ),
    pytest.param(
        _with(RUN, SecurityOpt=["seccomp=unconfined"]), "run takes exactly", id="extra-SecurityOpt"
    ),
    pytest.param(
        _with(BUILD, Mounts=[{"Type": "bind", "Source": "/"}]),
        "build takes exactly",
        id="build-extra-Mounts",
    ),
    pytest.param(
        _with(BUILD, SecurityOpt=["apparmor=unconfined"]),
        "build takes exactly",
        id="build-extra-SecurityOpt",
    ),
    pytest.param(
        _with(RUN, cpus=1e-10),
        "cpus: a number from 0.01 to 64 .LAKANA_LAUNCHER__MAX_CPUS",
        id="cpus-tiny",
    ),
    pytest.param(_with(RUN, cpus=5e-324), "cpus:", id="cpus-denormal"),
    pytest.param(_with(RUN, cpus=0.001), "cpus:", id="cpus-below-floor"),
    pytest.param(_with(RUN, cpus=-1), "cpus:", id="cpus-negative"),
    pytest.param(_with(RUN, cpus=-0.5), "cpus:", id="cpus-negative-float"),
    pytest.param(_with(RUN, cpus=64.5), "LAKANA_LAUNCHER__MAX_CPUS", id="cpus-over-ceiling"),
    pytest.param(
        _with(RUN, memory_mb=10**9), "LAKANA_LAUNCHER__MAX_MEMORY_MB", id="memory-names-setting"
    ),
    pytest.param(_with(RUN, pids=10**9), "LAKANA_LAUNCHER__MAX_PIDS", id="pids-names-setting"),
    pytest.param(
        _with(RUN, token="\ud800"), "does not carry the launcher's token", id="token-surrogate"
    ),
    pytest.param(_with(RUN, stdin="a\ud800"), "stdin:", id="stdin-surrogate"),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {"url": "https://pkgs.example.org/simple", "username": "", "password": "a\ud800"}
            ],
        ),
        "indexes:",
        id="password-surrogate",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {"url": "https://pkgs.example.org/simple", "username": "\udfff", "password": "p"}
            ],
        ),
        "indexes:",
        id="username-surrogate",
    ),
    pytest.param(_with(RUN, owner="w\ud800"), "owner:", id="owner-surrogate"),
    pytest.param(
        _with(
            BUILD,
            operator_index="https://op:pw@pkgs.example.org\\@evil.org/",
            hosts=["pkgs.example.org", "evil.org"],
        ),
        "operator_index:",
        id="operator-backslash",
    ),
    pytest.param(_with(BUILD, hosts=["127.0.0.1"], indexes=[]), "hosts:", id="host-loopback"),
    pytest.param(_with(BUILD, hosts=["169.254.169.254"], indexes=[]), "hosts:", id="host-metadata"),
    pytest.param(_with(BUILD, hosts=["10.1.2.3"], indexes=[]), "hosts:", id="host-rfc1918-10"),
    pytest.param(_with(BUILD, hosts=["172.16.0.1"], indexes=[]), "hosts:", id="host-rfc1918-172"),
    pytest.param(_with(BUILD, hosts=["192.168.0.1"], indexes=[]), "hosts:", id="host-rfc1918-192"),
    pytest.param(_with(BUILD, hosts=["0.0.0.0"], indexes=[]), "hosts:", id="host-unspecified"),
    pytest.param(_with(BUILD, hosts=["::1"], indexes=[]), "hosts:", id="host-ipv6-loopback"),
    pytest.param(_with(BUILD, hosts=["fd00::1"], indexes=[]), "hosts:", id="host-ipv6-ula"),
    pytest.param(_with(BUILD, hosts=["fe80::1"], indexes=[]), "hosts:", id="host-ipv6-link-local"),
    pytest.param(_with(BUILD, hosts=["::"], indexes=[]), "hosts:", id="host-ipv6-unspecified"),
    pytest.param(_with(BUILD, hosts=["127.1"], indexes=[]), "hosts:", id="host-short-ipv4"),
    pytest.param(_with(BUILD, hosts=["127.0.0.0x1"], indexes=[]), "hosts:", id="host-mixed-hex"),
    pytest.param(_with(BUILD, hosts=["0.0x0.0.0"], indexes=[]), "hosts:", id="host-hex-label"),
    pytest.param(_with(BUILD, hosts=["100.64.0.1"], indexes=[]), "hosts:", id="host-shared-space"),
    pytest.param(_with(BUILD, hosts=["224.0.0.1"], indexes=[]), "hosts:", id="host-multicast"),
    pytest.param(_with(RUN, op="exec"), "op must be one of", id="op-exec"),
    pytest.param(
        _with(RUN, token="x" * 64), "does not carry the launcher's token", id="wrong-token"
    ),
    pytest.param(_without(RUN, "token"), "does not carry the launcher's token", id="no-token"),
    pytest.param([1, 2], "one JSON object", id="a-list"),
    pytest.param("run", "one JSON object", id="a-string"),
    pytest.param(
        {"op": "stop", "token": TOKEN, "id": "../lakana-server"}, "id:", id="stop-traversal"
    ),
    pytest.param(
        {"op": "stop", "token": TOKEN, "id": "a", "force": True},
        "stop takes exactly",
        id="stop-extra",
    ),
    pytest.param({"op": "cleanup", "token": TOKEN, "owner": ""}, "owner:", id="cleanup-empty"),
    pytest.param(
        {"op": "status", "token": TOKEN, "python": "3.13", "environment": "x"},
        "environment:",
        id="status-key",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[{"url": "http://pkgs.example.org/simple", "username": "", "password": "p"}],
        ),
        "indexes:",
        id="index-http",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {"url": "https://u:p@pkgs.example.org/simple", "username": "", "password": "p"}
            ],
        ),
        "indexes:",
        id="index-userinfo",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {"url": "https://pkgs.example.org/simple?x=1", "username": "", "password": "p"}
            ],
        ),
        "indexes:",
        id="index-query",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[{"url": "https://other.example.org/simple", "username": "", "password": "p"}],
        ),
        "every index's host must be in hosts",
        id="index-host-not-allowed",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[{"url": "https://pkgs.example.org/simple", "username": "", "password": ""}],
        ),
        "indexes:",
        id="index-empty-password",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {"url": "https://pkgs.example.org/simple", "username": "", "password": "a\x00b"}
            ],
        ),
        "indexes:",
        id="index-nul",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {
                    "url": "https://pkgs.example.org/simple",
                    "username": "",
                    "password": "p",
                    "env": "LD_PRELOAD=x",
                }
            ],
        ),
        "indexes:",
        id="index-extra-key",
    ),
    pytest.param(
        _with(BUILD, indexes=[BUILD["indexes"][0]] * 17), "indexes:", id="too-many-indexes"
    ),
    pytest.param(_with(BUILD, hosts=["PKGS.example.org"]), "hosts:", id="host-upper-case"),
    pytest.param(_with(BUILD, hosts=["pkgs.example.org:8443"]), "hosts:", id="host-with-port"),
    pytest.param(_with(BUILD, hosts=[]), "hosts:", id="no-host"),
    pytest.param(
        _with(BUILD, operator_index="https://in dex/simple"), "operator_index:", id="operator-space"
    ),
    pytest.param(
        _with(BUILD, operator_index="file:///etc/passwd"), "operator_index:", id="operator-file"
    ),
    pytest.param(_with(BUILD, no_build="yes"), "no_build:", id="no-build-text"),
    pytest.param(_with(BUILD, max_bytes=0), "max_bytes:", id="max-bytes-zero"),
]


@pytest.mark.parametrize(("request_", "reason"), HOSTILE)
def test_every_hostile_request_shape_is_refused(request_: object, reason: str) -> None:
    """Nothing outside the schema reaches a container spec.

    Parameters
    ----------
    request_
        The request as the client would send it.
    reason
        Part of the refusal's text.
    """
    with pytest.raises(Refused, match=reason):
        parse_request(_raw(request_), token=TOKEN, settings=SETTINGS)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        pytest.param(b"x" * (MAX_REQUEST_BYTES + 1), "512 KiB", id="too-large"),
        pytest.param(_raw({**RUN, "cpus": "@@"}).replace(b'"@@"', b"NaN"), "without NaN", id="nan"),
        pytest.param(
            _raw({**RUN, "cpus": "@@"}).replace(b'"@@"', b"Infinity"),
            "without Infinity",
            id="infinity",
        ),
        pytest.param(b"[" * 100_000, "one JSON object", id="deep"),
        pytest.param(b"\xff\xfe", "one JSON object", id="not-utf8"),
        pytest.param(b"", "one JSON object", id="empty"),
    ],
)
def test_a_request_that_is_not_one_bounded_json_object_is_refused(raw: bytes, reason: str) -> None:
    """Size, constants, depth and encoding are refused before any field is read.

    The NaN and Infinity cases carry a valid token, so only the constant can refuse them.

    Parameters
    ----------
    raw
        The bytes on the socket.
    reason
        Part of the refusal's text.
    """
    with pytest.raises(Refused, match=reason):
        parse_request(raw, token=TOKEN, settings=SETTINGS)


def test_the_valid_requests_parse_into_their_plain_values() -> None:
    """The five operations, as the worker sends them."""
    run = parse_request(_raw(RUN), token=TOKEN, settings=SETTINGS)
    build = parse_request(_raw(BUILD), token=TOKEN, settings=SETTINGS)
    stop = parse_request(
        _raw({"token": TOKEN, "op": "stop", "id": "0f3c9a1e"}), token=TOKEN, settings=SETTINGS
    )
    status = parse_request(
        _raw({"token": TOKEN, "op": "status", "python": "3.13", "environment": KEY}),
        token=TOKEN,
        settings=SETTINGS,
    )
    cleanup = parse_request(
        _raw({"token": TOKEN, "op": "cleanup", "owner": "worker-1"}), token=TOKEN, settings=SETTINGS
    )

    assert (
        isinstance(run, RunRequest)
        and run.stdin == b'{"version": 2}'
        and run.limits.cpus == pytest.approx(1.0)
    )
    assert isinstance(build, BuildRequest) and build.indexes[0].host == "pkgs.example.org"
    assert "p@ss word" not in repr(build)
    assert isinstance(stop, StopRequest) and stop.id == "0f3c9a1e"
    assert isinstance(status, StatusRequest) and status.environment == KEY
    assert isinstance(cleanup, CleanupRequest) and cleanup.owner == "worker-1"


def test_the_run_spec_fixes_every_field_that_matters() -> None:
    """User, capabilities, mounts, network, command and image come from the launcher alone."""
    run = parse_request(_raw(_with(RUN, roots=["landing"])), token=TOKEN, settings=SETTINGS)
    assert isinstance(run, RunRequest)

    spec = run_container_spec(run, SETTINGS, no_new_privileges=True)

    host = spec["HostConfig"]
    assert spec["Image"] == "ghcr.io/chrschy/lakana-task:0.14.0-py3.13"
    assert spec["Entrypoint"] == ["/env/bin/python", "-P", "-m", "lakana.runner"]
    assert spec["User"] == "10001:10001"
    assert spec["WorkingDir"] == "/code"
    assert (host["CapDrop"], host["CapAdd"], host["Privileged"]) == (["ALL"], [], False)
    assert (host["ReadonlyRootfs"], host["Init"], host["IpcMode"], host["PidMode"]) == (
        True,
        True,
        "private",
        "",
    )
    assert host["NetworkMode"] == "lakana-task-0f3c9a1e"
    assert (host["Binds"], host["Devices"]) == ([], [])
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert (host["Memory"], host["MemorySwap"]) == (2048 * 2**20, 2048 * 2**20)
    assert (host["NanoCpus"], host["PidsLimit"]) == (1_000_000_000, 256)
    assert [(m["Type"], m["Source"], m["Target"], m["ReadOnly"]) for m in host["Mounts"]] == [
        ("volume", "lakana-task-work", "/code", True),
        ("volume", environment_volume(SETTINGS, "3.13", KEY), "/env", True),
        ("volume", "lakana-task-work", "/work", False),
        ("bind", "/srv/landing", "/ingest/landing", True),
    ]
    assert [m.get("VolumeOptions", {}).get("Subpath") for m in host["Mounts"]] == [
        "attempt-abc_12/code",
        None,
        "attempt-abc_12/task",
        None,
    ]
    assert not any("token" in variable.lower() for variable in spec["Env"])
    assert spec["Labels"][LABEL_STACK] == "lakana" and spec["Labels"][LABEL_OWNER] == "worker-1"
    assert "POLARS_OVERRIDE_TOTAL_MEMORY_MB=2147" in spec["Env"]
    assert (
        run_container_spec(run, SETTINGS, no_new_privileges=False)["HostConfig"]["SecurityOpt"]
        == []
    )


def test_the_build_spec_mounts_only_code_environment_and_cache() -> None:
    """The build writes its own volume and the cache; the credential is an environment value."""
    build = parse_request(_raw(BUILD), token=TOKEN, settings=SETTINGS)
    assert isinstance(build, BuildRequest)

    spec = build_container_spec(build, SETTINGS, no_new_privileges=False, proxy_url=None)

    host = spec["HostConfig"]
    assert spec["User"] == "10002:10002"
    assert spec["Entrypoint"] == ["/usr/local/bin/python", "-I", "/opt/lakana-task/build.py"]
    assert (host["CapDrop"], host["CapAdd"], host["Privileged"]) == (["ALL"], [], False)
    assert (host["ReadonlyRootfs"], host["PidMode"], host["IpcMode"]) == (True, "", "private")
    assert (host["Binds"], host["Devices"]) == ([], [])
    assert [(m["Source"], m["Target"], m["ReadOnly"]) for m in host["Mounts"]] == [
        ("lakana-task-work", "/src", True),
        (environment_volume(SETTINGS, "3.13", KEY), "/env", False),
        (cache_volume(SETTINGS, build), "/uv-cache", False),
    ]
    assert (
        "UV_INDEX=lakana-registered-0=https://ci:p%40ss%20word@pkgs.example.org/simple"
        in spec["Env"]
    )
    assert "LAKANA_BUILD_NO_BUILD=1" in spec["Env"]
    assert not any(v.startswith(("HTTPS_PROXY=", "https_proxy=")) for v in spec["Env"])
    assert host["NetworkMode"] == "lakana-build-b0f3c9a1e"
    with_proxy = build_container_spec(
        build, SETTINGS, no_new_privileges=False, proxy_url="http://lakana:tok@egress:3128"
    )
    assert "HTTPS_PROXY=http://lakana:tok@egress:3128" in with_proxy["Env"]


def test_a_task_network_is_internal_and_has_no_gateway_on_the_host() -> None:
    """The trial's finding: `internal` alone still reaches the host through the gateway."""
    spec = network_spec(SETTINGS, "task", "0f3c9a1e", "worker-1")

    assert spec["Name"] == "lakana-task-0f3c9a1e"
    assert spec["Internal"] is True
    assert spec["Options"]["com.docker.network.bridge.gateway_mode_ipv4"] == "isolated"
    assert spec["Options"]["com.docker.network.bridge.gateway_mode_ipv6"] == "isolated"


def test_volume_names_change_with_the_image_and_the_index_set() -> None:
    """A venv links into its image, and a cache serves one set of indexes."""
    build = parse_request(_raw(BUILD), token=TOKEN, settings=SETTINGS)
    other = parse_request(
        _raw(
            _with(BUILD, hosts=["pkgs.example.org", "pypi.org", "files.pythonhosted.org", "x.org"])
        ),
        token=TOKEN,
        settings=SETTINGS,
    )
    newer = LauncherSettings(
        stack="lakana",
        images={"3.13": "ghcr.io/chrschy/lakana-task:0.15.0-py3.13"},
        work_volume="lakana-task-work",
    )
    assert isinstance(build, BuildRequest) and isinstance(other, BuildRequest)

    assert environment_volume(SETTINGS, "3.13", KEY) != environment_volume(newer, "3.13", KEY)
    assert environment_volume(SETTINGS, "3.13", KEY).startswith("lakana-env-")
    assert cache_volume(SETTINGS, build) != cache_volume(SETTINGS, other)


def test_the_operator_index_comes_first_and_keeps_its_own_credential() -> None:
    """`LAKANA_RUN__INDEX_URL` may carry a password; it is passed as the operator wrote it."""
    build = parse_request(
        _raw(
            _with(
                BUILD,
                operator_index="https://op:pw@mirror.example.org/simple",
                hosts=["pkgs.example.org", "mirror.example.org"],
            )
        ),
        token=TOKEN,
        settings=SETTINGS,
    )
    assert isinstance(build, BuildRequest)

    assert uv_index_value(build) == (
        "https://op:pw@mirror.example.org/simple "
        "lakana-registered-0=https://ci:p%40ss%20word@pkgs.example.org/simple"
    )


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        pytest.param(
            b'{"op": "stop", "op": "run", "token": "' + TOKEN.encode() + b'", "id": "a"}',
            "repeats the field",
            id="repeated-op",
        ),
        pytest.param(
            b'{"op": "stop", "token": "' + TOKEN.encode() + b'", "id": "a", "id": "b"}',
            "repeats the field",
            id="repeated-id",
        ),
    ],
)
def test_a_repeated_json_key_is_refused(raw: bytes, reason: str) -> None:
    """The last of two equal keys must not silently win.

    Parameters
    ----------
    raw
        The bytes on the socket.
    reason
        Part of the refusal's text.
    """
    with pytest.raises(Refused, match=reason):
        parse_request(raw, token=TOKEN, settings=SETTINGS)


def test_public_ip_literals_and_host_names_are_allowed_hosts() -> None:
    """Only loopback, link-local, private and unspecified literals are refused."""
    build = parse_request(
        _raw(_with(BUILD, hosts=["8.8.8.8", "pkgs.example.org"])), token=TOKEN, settings=SETTINGS
    )
    assert isinstance(build, BuildRequest)


def test_the_smallest_cpu_share_is_a_real_limit() -> None:
    """A share Docker would read as no limit never reaches a spec."""
    run = parse_request(_raw(_with(RUN, cpus=0.01)), token=TOKEN, settings=SETTINGS)
    assert isinstance(run, RunRequest)

    assert run_container_spec(run, SETTINGS, no_new_privileges=True)["HostConfig"]["NanoCpus"] == (
        10_000_000
    )


def test_every_spec_overrides_the_entrypoint_with_its_own_command() -> None:
    """The image's own entrypoint never wraps the launcher's command."""
    run = parse_request(_raw(RUN), token=TOKEN, settings=SETTINGS)
    build = parse_request(_raw(BUILD), token=TOKEN, settings=SETTINGS)
    assert isinstance(run, RunRequest) and isinstance(build, BuildRequest)

    specs = [
        run_container_spec(run, SETTINGS, no_new_privileges=True),
        build_container_spec(build, SETTINGS, no_new_privileges=True, proxy_url=None),
        probe_container_spec(SETTINGS, "ghcr.io/chrschy/lakana-task:0.14.0-py3.13"),
    ]
    for spec in specs:
        assert spec["Cmd"] == []
        assert spec["Entrypoint"][0].startswith("/")
    assert specs[0]["Entrypoint"] == ["/env/bin/python", "-P", "-m", "lakana.runner"]
    assert specs[1]["Entrypoint"] == ["/usr/local/bin/python", "-I", "/opt/lakana-task/build.py"]
