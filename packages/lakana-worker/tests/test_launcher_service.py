"""`Launcher`: start-up checks, the five operations, and what it removes, against a fake engine."""
# ruff: file-ignore[any-type]  (Docker's JSON bodies and the tests' hooks are untyped by nature)

from __future__ import annotations

import base64
import io
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from launcher_fakes import FakeEngine, settings

from lakana_worker.launcher.engine import EngineError
from lakana_worker.launcher.requests import (
    INGEST_PROBE_SCRIPT,
    BuildRequest,
    CleanupRequest,
    Limits,
    Refused,
    RunRequest,
    StatusRequest,
    StopRequest,
    container_name,
    environment_volume,
)
from lakana_worker.launcher.service import Launcher, StartError, read_frames

KEY = "a" * 64
LIMITS = Limits(memory_mb=256, cpus=1.0, pids=64, timeout_seconds=30)


def _run(task: str = "t1", owner: str = "w1", timeout: int = 30) -> RunRequest:
    """Build a run request.

    Parameters
    ----------
    task
        The task id.
    owner
        The worker.
    timeout
        Its timeout.

    Returns
    -------
    RunRequest
        The request.
    """
    return RunRequest(
        task=task,
        owner=owner,
        python="3.13",
        environment=KEY,
        attempt="attempt-x1",
        limits=Limits(256, 1.0, 64, timeout),
        kill_grace_seconds=3,
        stdin=b'{"v": 2}',
    )


def _build(build: str = "b1", key: str = KEY) -> BuildRequest:
    """Build a build request.

    Parameters
    ----------
    build
        The build id.
    key
        The environment key.

    Returns
    -------
    BuildRequest
        The request.
    """
    return BuildRequest(
        build=build,
        owner="w1",
        python="3.13",
        environment=key,
        attempt="attempt-x1",
        indexes=(),
        operator_index=None,
        hosts=("pypi.org",),
        no_build=False,
        limits=LIMITS,
        max_bytes=1 << 30,
    )


def _started(engine: FakeEngine, tmp_path: Path, *, built: bool = True, **changes: Any) -> Launcher:
    """Build and start a launcher.

    Parameters
    ----------
    engine
        The fake engine.
    tmp_path
        The state directory.
    built
        Whether the environment of `KEY` counts as built already (a run needs it).
    **changes
        Settings to replace.

    Returns
    -------
    Launcher
        The started launcher.
    """
    launcher = Launcher(settings(tmp_path, **changes), engine)
    launcher.start()
    if built:
        _built(launcher, engine)
    return launcher


def _built(launcher: Launcher, engine: FakeEngine, key: str = KEY) -> None:
    """Make an environment count as built: its marker exists and its volume does.

    Parameters
    ----------
    launcher
        The started launcher.
    engine
        Its fake engine.
    key
        The environment key.
    """
    volume = environment_volume(launcher.settings, "3.13", key)
    launcher._mark_complete(volume)
    engine.volumes.add(volume)


def _logs(emitted: list[dict[str, object]]) -> bytes:
    """Join the log lines a run emitted.

    Parameters
    ----------
    emitted
        What the launcher emitted.

    Returns
    -------
    bytes
        The decoded output.
    """
    return b"".join(base64.b64decode(str(e["log"])) for e in emitted if "log" in e)


def test_start_refuses_an_engine_older_than_28(tmp_path: Path) -> None:
    """Without the isolated gateway mode a task network would reach the host."""
    with pytest.raises(StartError, match="Docker Engine 28 or later is needed") as caught:
        _started(FakeEngine(version="27.5.1"), tmp_path)
    assert "27.5.1" in str(caught.value)


def test_start_removes_what_an_earlier_launcher_left(tmp_path: Path) -> None:
    """Owners run their tasks again; the launcher keeps nothing from before its start."""
    engine = FakeEngine()
    engine.containers["old"] = {
        "Name": "lakana-task-old",
        "exit": None,
        "spec": {"HostConfig": {"Mounts": []}},
        "Labels": {"lakana.launcher.stack": "lakana"},
    }
    engine._released["old"] = threading.Event()
    engine.networks["lakana-task-old"] = {
        "Labels": {"lakana.launcher.stack": "lakana"},
        "Containers": {},
    }

    _started(engine, tmp_path)

    assert "old" not in engine.containers and "lakana-task-old" not in engine.networks


def test_no_new_privileges_auto_stays_off_where_docker_refuses_it(tmp_path: Path) -> None:
    """Snap Docker refuses every exec with the option; auto turns it off there."""
    engine = FakeEngine(no_new_privileges_works=False)
    launcher = _started(engine, tmp_path)

    launcher.run(_run(), lambda line: None)

    assert launcher.no_new_privileges is False
    assert engine.created("task")[0]["HostConfig"]["SecurityOpt"] == []
    on = _started(FakeEngine(), tmp_path)
    assert on.no_new_privileges is True


def test_no_new_privileges_on_refuses_to_start_where_it_breaks(tmp_path: Path) -> None:
    """`on` is a promise: the launcher does not start without the option."""
    with pytest.raises(StartError, match="NO_NEW_PRIVILEGES is on"):
        _started(FakeEngine(no_new_privileges_works=False), tmp_path, no_new_privileges="on")


def test_a_run_streams_output_and_removes_its_container_and_network(tmp_path: Path) -> None:
    """The task gets its description on stdin, the server joins its network as `server`."""
    engine = FakeEngine(output=[b"hel", b"lo\n"])
    launcher = _started(engine, tmp_path)
    emitted: list[dict[str, object]] = []
    network = container_name(launcher.settings, "task", "t1")
    seen: dict[str, Any] = {}
    original = engine._network

    def spy(method: str, name: str, action: str | None, body: Any) -> tuple[int, Any]:
        if action == "connect":
            seen[name] = body
        return original(method, name, action, body)

    engine._network = spy  # ty: ignore[invalid-assignment]

    result = launcher.run(_run(), emitted.append)

    assert result == {"exit": 0, "oom_killed": False, "timed_out": False, "stopped": False}
    assert _logs(emitted) == b"hello\n"
    assert engine.stdin == [b'{"v": 2}']
    assert seen[network] == {"Container": "c-server", "EndpointConfig": {"Aliases": ["server"]}}
    assert [
        c for c in engine.containers.values() if c["Labels"]["lakana.launcher.kind"] == "task"
    ] == []
    assert network not in engine.networks


def test_a_run_reports_an_out_of_memory_kill_and_the_exit_code(tmp_path: Path) -> None:
    """`OOMKilled` reaches the worker, which maps it to the memory stop reason."""
    engine = FakeEngine(exit_code=137, oom=True)

    result = _started(engine, tmp_path).run(_run(), lambda line: None)

    assert (result["exit"], result["oom_killed"]) == (137, True)


def test_the_timeout_stops_the_container_with_its_grace(tmp_path: Path) -> None:
    """At the timeout the launcher asks Docker to stop the container, SIGKILL after the grace."""
    engine = FakeEngine(hold=True)

    result = _started(engine, tmp_path).run(_run(timeout=1), lambda line: None)

    assert result["timed_out"] is True and result["exit"] == 137
    stops = [(p, q) for m, p, q in engine.calls if p.endswith("/stop")]
    assert stops and stops[0][1] == {"t": "3"}


def test_stop_stops_a_running_task(tmp_path: Path) -> None:
    """A `stop` from another connection ends a `run` that is waiting on its container."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.run(_run(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))

    assert launcher.stop(StopRequest("t1")) == {"stopped": True}
    worker.join(10)

    assert results[0]["stopped"] is True
    assert launcher.stop(StopRequest("t1")) == {"stopped": False}


def test_a_closed_connection_removes_the_container_and_its_network(tmp_path: Path) -> None:
    """Review focus: a worker that dies sends nothing; the launcher still cleans up."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)

    def gone(line: dict[str, object]) -> None:
        raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        launcher.run(_run(), gone)
    assert engine.containers == {} and engine.networks == {}

    quiet = FakeEngine(hold=True)
    launcher = _started(quiet, tmp_path)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.run(_run(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(quiet))
    launcher.abandon(container_name(launcher.settings, "task", "t1"))
    worker.join(10)
    assert results[0]["stopped"] is True
    assert quiet.containers == {} and quiet.networks == {}
    assert any(p.endswith("/kill") for _, p, _ in quiet.calls)


def test_cleanup_spares_a_container_whose_connection_is_alive(tmp_path: Path) -> None:
    """Review focus: another replica's running task is not an orphan."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    worker = threading.Thread(target=lambda: launcher.run(_run(owner="w1"), lambda line: None))
    worker.start()
    _until(lambda: _task_started(engine))
    engine.containers["orphan"] = {
        "Name": "lakana-task-orphan",
        "exit": None,
        "spec": {"HostConfig": {"Mounts": []}},
        "Labels": {"lakana.launcher.stack": "lakana", "lakana.launcher.owner": "w1"},
    }
    engine._released["orphan"] = threading.Event()

    assert launcher.cleanup(CleanupRequest("w1")) == {"removed": 1}

    assert "orphan" not in engine.containers
    assert any(c["Name"] == "lakana-task-t1" for c in engine.containers.values())
    launcher.stop(StopRequest("t1"))
    worker.join(10)


def test_two_builds_of_one_environment_run_once(tmp_path: Path) -> None:
    """Review focus: the second request waits for the first and finds the environment built."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    results: list[dict[str, object]] = []
    threads = [
        threading.Thread(
            target=lambda b=b: results.append(launcher.build(_build(b), lambda line: None))
        )
        for b in ("b1", "b2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert [r["complete"] for r in results] == [True, True]
    assert len(engine.created("build")) == 1
    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": True}


def test_a_failed_build_leaves_no_marker_and_no_volume(tmp_path: Path) -> None:
    """Review focus: the next build of the same environment starts clean."""
    engine = FakeEngine(exit_code=1)
    launcher = _started(engine, tmp_path, built=False)
    volume = environment_volume(launcher.settings, "3.13", KEY)

    result = launcher.build(_build(), lambda line: None)

    assert result["complete"] is False and result["exit"] == 1
    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": False}
    deletes = [p for m, p, _ in engine.calls if m == "DELETE" and p == f"/volumes/{volume}"]
    assert len(deletes) == 2  # once before the build, once after it failed
    engine.exit_code = 0
    assert launcher.build(_build("b2"), lambda line: None)["complete"] is True


def test_old_environments_are_removed_beyond_the_ones_kept(tmp_path: Path) -> None:
    """With one kept, building a second removes the first's volume and marker."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, keep_environments=1, built=False)
    other = "b" * 64

    launcher.build(_build("b1", KEY), lambda line: None)
    time.sleep(0.01)
    launcher.build(_build("b2", other), lambda line: None)

    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": False}
    assert launcher.status(StatusRequest("3.13", other)) == {"complete": True}


def test_a_run_without_a_running_server_names_the_service(tmp_path: Path) -> None:
    """The worker reports this as "runner unavailable", with the text."""
    engine = FakeEngine(services={})
    launcher = _started(engine, tmp_path)

    with pytest.raises(EngineError, match="the server service of this stack is not running"):
        launcher.run(_run(), lambda line: None)
    assert engine.networks == {}


def test_a_missing_image_is_pulled_once(tmp_path: Path) -> None:
    """Only an allowed reference is ever pulled."""
    engine = FakeEngine(images=set())

    _started(engine, tmp_path).run(_run(), lambda line: None)

    pulls = [q for m, p, q in engine.calls if p == "/images/create"]
    assert pulls == [{"fromImage": "ghcr.io/chrschy/lakana-task:0.14.0-py3.13"}]


def _intercept(engine: FakeEngine, match: Any, reply: Any) -> None:
    """Answer the calls `match` selects with `reply` (raised when it is an exception).

    Parameters
    ----------
    engine
        The fake engine.
    match
        Takes method, path and body; says whether to intercept.
    reply
        The answer, or the exception to raise.
    """
    real = engine.request

    def request(
        method: str, path: str, body: Any = None, *, query: Any = None, timeout: Any = 60.0
    ) -> tuple[int, Any]:
        if match(method, path, body):
            if isinstance(reply, Exception):
                raise reply
            return reply
        return real(method, path, body, query=query, timeout=timeout)

    engine.request = request  # ty: ignore[invalid-assignment]


def _starts(engine: FakeEngine) -> int:
    """Count the containers started so far.

    Parameters
    ----------
    engine
        The fake engine.

    Returns
    -------
    int
        How many `/start` calls were made.
    """
    return sum(path.endswith("/start") for _, path, _ in engine.calls)


def test_start_refuses_an_engine_that_cannot_make_an_isolated_network(tmp_path: Path) -> None:
    """Without it a task network reaches the host's services through its gateway."""
    with pytest.raises(StartError, match="isolated network") as caught:
        _started(FakeEngine(isolated="refused"), tmp_path)
    assert "29.8.0" in str(caught.value) and "invalid option gateway_mode_ipv4" in str(caught.value)


def test_start_refuses_an_engine_that_drops_the_isolated_option(tmp_path: Path) -> None:
    """An engine that accepts the network but forgets the option is as bad as one that refuses."""
    engine = FakeEngine(isolated="ignored")

    with pytest.raises(StartError, match="did not keep gateway_mode_ipv4=isolated") as caught:
        _started(engine, tmp_path)

    assert "29.8.0" in str(caught.value)
    assert engine.networks == {}


def test_start_leaves_no_check_network_behind(tmp_path: Path) -> None:
    """The network made to prove the engine can isolate is removed again."""
    engine = FakeEngine()

    _started(engine, tmp_path)

    assert engine.networks == {}
    assert any(p == "/networks/create" for _, p, _ in engine.calls)


def _roots(tmp_path: Path, engine: FakeEngine) -> Launcher:
    """Start a launcher with one ingestion root, named `data`.

    Parameters
    ----------
    tmp_path
        The state directory.
    engine
        The fake engine.

    Returns
    -------
    Launcher
        The started launcher.
    """
    return _started(engine, tmp_path, ingestion_roots={"data": "/srv/data"})


def test_an_ingestion_root_is_probed_by_a_container_that_is_then_removed(tmp_path: Path) -> None:
    """The probe sees the root as a task would: read-only, non-recursive, no network."""
    engine = FakeEngine()

    _roots(tmp_path, engine)

    probe = engine.created("probe")[-1]
    assert probe["Labels"]["lakana.launcher.id"] == "ingest-data"
    assert probe["HostConfig"]["Mounts"] == [
        {
            "Type": "bind",
            "Source": "/srv/data",
            "Target": "/ingest/data",
            "ReadOnly": True,
            "BindOptions": {"Propagation": "rprivate", "NonRecursive": True},
        }
    ]
    assert probe["NetworkDisabled"] is True
    assert probe["Cmd"] == ["/ingest/data", "/proc/self/mountinfo", "100000"]
    assert engine.containers == {}


def _reason(tmp_path: Path, engine: FakeEngine) -> str:
    """Start with the root `data` and return why it was marked unusable.

    Parameters
    ----------
    tmp_path
        The state directory.
    engine
        The fake engine.

    Returns
    -------
    str
        The reason the launcher recorded for `data`.
    """
    return _roots(tmp_path, engine).unusable_roots["data"]


def test_a_root_with_a_socket_is_unusable_and_its_reason_never_names_its_path(
    tmp_path: Path,
) -> None:
    """A read-only mount does not stop a task from connecting to a socket under it."""
    reason = _reason(tmp_path, FakeEngine(probe_exits={"data": 4}))

    assert "ingestion root 'data' holds a socket" in reason
    assert "/srv/data" not in reason and "INGESTION_ROOTS" in reason


def test_a_root_that_is_a_system_mount_is_unusable(tmp_path: Path) -> None:
    """A root behind a symlink to /proc, /sys or /dev would hand the host to every task."""
    reason = _reason(tmp_path, FakeEngine(probe_exits={"data": 3}))

    assert "proc, sysfs or devtmpfs" in reason and "ingestion root 'data'" in reason


def test_a_root_past_the_entry_cap_is_unusable_and_the_reason_names_the_setting(
    tmp_path: Path,
) -> None:
    """The cap is a setting, and the message says which."""
    reason = _reason(tmp_path, FakeEngine(probe_exits={"data": 5}))

    assert "ingestion root 'data' has more than 100000 entries" in reason
    assert "LAKANA_LAUNCHER__PROBE_MAX_ENTRIES" in reason


def test_the_probe_gets_the_entry_cap_from_the_settings(tmp_path: Path) -> None:
    """The container's script reads the cap as its third argument."""
    engine = FakeEngine()

    _started(engine, tmp_path, ingestion_roots={"data": "/srv/data"}, probe_max_entries=7)

    assert engine.created("probe")[-1]["Cmd"] == ["/ingest/data", "/proc/self/mountinfo", "7"]


def test_a_root_with_something_unreadable_is_unusable_and_the_reason_says_what_to_do(
    tmp_path: Path,
) -> None:
    """`lost+found` at the top of a filesystem is the usual cause."""
    reason = _reason(tmp_path, FakeEngine(probe_exits={"data": 6}))

    assert "ingestion root 'data' holds something the task user" in reason
    assert "lost+found" in reason and "subdirectory" in reason and "10001" in reason
    assert "/srv/data" not in reason


def test_a_root_that_could_not_be_checked_is_unusable(tmp_path: Path) -> None:
    """Any other probe result marks the root: an unchecked root is not a safe one."""
    reason = _reason(tmp_path, FakeEngine(probe_exits={"data": 1}))

    assert "ingestion root 'data' could not be checked" in reason


def test_the_launcher_starts_with_an_unusable_root_and_refuses_only_the_requests_naming_it(
    tmp_path: Path,
) -> None:
    """Every task that names no such root keeps working; the refusal carries the reason."""
    engine = FakeEngine(probe_exits={"data": 6})
    launcher = _roots(tmp_path, engine)

    with pytest.raises(Refused, match="ingestion root 'data' holds something the task user"):
        launcher.run(replace(_run(), roots=("data",)), lambda line: None)

    assert engine.created("task") == [] and launcher._live == {}
    assert launcher.run(_run(), lambda line: None)["exit"] == 0


def test_a_usable_root_still_works_beside_an_unusable_one(tmp_path: Path) -> None:
    """The marking is per root."""
    engine = FakeEngine(probe_exits={"bad": 4})
    launcher = _started(engine, tmp_path, ingestion_roots={"good": "/srv/good", "bad": "/srv/bad"})

    assert set(launcher.unusable_roots) == {"bad"}
    assert launcher.run(replace(_run(), roots=("good",)), lambda line: None)["exit"] == 0


def test_a_probe_docker_refuses_marks_the_root_unusable_with_dockers_reason(
    tmp_path: Path,
) -> None:
    """A root that does not exist on the host fails the probe's create; the reason says so."""
    engine = FakeEngine()
    _intercept(
        engine,
        lambda m, p, b: (
            p == "/containers/create" and b["Labels"]["lakana.launcher.id"] == "ingest-data"
        ),
        (400, {"message": "bind source path does not exist"}),
    )

    reason = _reason(tmp_path, engine)

    assert "cannot check ingestion root 'data'" in reason
    assert "bind source path does not exist" in reason


def test_a_probe_that_does_not_start_marks_the_root_unusable_and_is_removed(
    tmp_path: Path,
) -> None:
    """Docker may accept the create and refuse the start (a missing source, a bad mount)."""
    engine = FakeEngine()
    _intercept(
        engine,
        lambda m, p, b: p.endswith("/start") and "c2" in p,
        (400, {"message": "mount denied"}),
    )

    reason = _reason(tmp_path, engine)

    assert "cannot check ingestion root 'data'" in reason and "mount denied" in reason
    assert engine.containers == {}


def _probe(root: Path, mountinfo: Path, limit: int = 100000) -> int:
    """Run the ingestion probe's script on the host, as the container would run it.

    Parameters
    ----------
    root
        The directory to check.
    mountinfo
        The mountinfo file to read.
    limit
        The entry cap, as the launcher passes it.

    Returns
    -------
    int
        Its exit status.
    """
    return subprocess.run(
        [sys.executable, "-I", "-c", INGEST_PROBE_SCRIPT, str(root), str(mountinfo), str(limit)],
        check=False,
        capture_output=True,
    ).returncode


def test_the_probe_script_accepts_a_plain_directory_and_a_link_loop(tmp_path: Path) -> None:
    """Plain files, a symlink back to the root and a mount elsewhere are fine."""
    root = tmp_path / "data"
    (root / "a").mkdir(parents=True)
    (root / "a" / "link").symlink_to(root)
    (root / "x.sock").write_text("not a socket", encoding="utf-8")
    info = tmp_path / "info"
    info.write_text(
        "30 22 0:25 / /ingest/data rw - ext4 /dev/sda1 rw\n"
        "31 30 0:5 / /somewhere/else rw - proc proc rw\n"
        "garbage\n",
        encoding="utf-8",
    )

    assert _probe(root, info) == 0


def test_the_probe_script_refuses_a_system_mount_at_or_under_the_root(tmp_path: Path) -> None:
    """A proc, sysfs or devtmpfs mount exposes the host whatever the directory looks like."""
    root = tmp_path / "data"
    root.mkdir()
    for fstype in (
        "proc",
        "sysfs",
        "devtmpfs",
        "debugfs",
        "tracefs",
        "cgroup",
        "cgroup2",
        "bpf",
        "securityfs",
        "pstore",
        "configfs",
    ):
        info = tmp_path / "info"
        info.write_text(f"31 30 0:5 / {root}/x rw - {fstype} s rw\n", encoding="utf-8")
        assert _probe(root, info) == 3, fstype


def test_the_probe_script_finds_a_socket_below_the_root_or_as_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only mount does not stop `connect()`, so any socket refuses the root."""
    root = tmp_path / "data"
    (root / "a" / "b").mkdir(parents=True)
    info = tmp_path / "info"
    info.write_text("", encoding="utf-8")
    monkeypatch.chdir(root / "a" / "b")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("s.sock")
        assert _probe(root, info) == 4
        assert _probe(root / "a" / "b" / "s.sock", info) == 4


def test_the_probe_script_stops_walking_after_a_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree past the bound is refused, not passed: the part not seen could hold a socket."""
    root = tmp_path / "data"
    (root / "z").mkdir(parents=True)
    for number in range(6):
        (root / f"f{number}").write_text("", encoding="utf-8")
    info = tmp_path / "info"
    info.write_text("", encoding="utf-8")
    monkeypatch.chdir(root / "z")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("s.sock")
        assert _probe(root, info) == 4
        assert _probe(root, info, limit=5) == 5


def test_the_probe_script_refuses_what_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that cannot be listed can still be walked through by name: it is refused."""
    root = tmp_path / "data"
    (root / "closed").mkdir(parents=True)
    info = tmp_path / "info"
    info.write_text("", encoding="utf-8")
    (root / "closed").chmod(0o000)
    try:
        if os.access(root / "closed", os.R_OK):  # running as root: nothing is unreadable
            pytest.skip("the user can read every directory")
        assert _probe(root, info) == 6
    finally:
        (root / "closed").chmod(0o700)
    assert _probe(tmp_path / "missing", info) == 6
    plain_file = tmp_path / "file"
    plain_file.write_text("x", encoding="utf-8")
    assert _probe(plain_file, info) == 0


def _named(launcher: Launcher, kind: str = "task", ident: str = "t1") -> str:
    """Name a container of the launcher.

    Parameters
    ----------
    launcher
        The launcher.
    kind
        ``task`` or ``build``.
    ident
        The id.

    Returns
    -------
    str
        The name.
    """
    return container_name(launcher.settings, kind, ident)


def _started_count(engine: FakeEngine) -> int:
    """Count the task or build containers started, leaving out the start-up probes.

    Parameters
    ----------
    engine
        The fake engine.

    Returns
    -------
    int
        How many.
    """
    return _starts(engine) - 1


def test_a_caller_that_left_before_the_run_registered_never_starts_it(tmp_path: Path) -> None:
    """The watcher can fire before the request reaches `run`; the name is remembered."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    launcher.abandon(_named(launcher))

    result = launcher.run(_run(), lambda line: None)

    assert result["stopped"] is True and result["exit"] is None
    assert engine.created("task") == [] and _started_count(engine) == 0
    assert launcher.stop(StopRequest("t1")) == {"stopped": False}
    assert launcher.run(_run(), lambda line: None)["stopped"] is False  # the name was spent


def test_a_caller_that_left_during_the_image_pull_never_starts_the_task(tmp_path: Path) -> None:
    """A pull takes minutes; a disconnect in it used to leave the task running to its timeout."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    engine.images.clear()
    engine.hooks["pull"] = lambda: launcher.abandon(_named(launcher))

    result = launcher.run(_run(), lambda line: None)

    assert result["stopped"] is True
    assert engine.created("task") == [] and _started_count(engine) == 0
    assert engine.networks == {}


@pytest.mark.parametrize(
    ("moment", "leave"),
    [
        ("created", "stop"),
        ("created", "abandon"),
        ("attach", "stop"),
        ("attach", "abandon"),
    ],
)
def test_a_stop_between_create_and_start_leaves_nothing_running(
    tmp_path: Path, moment: str, leave: str
) -> None:
    """The container exists but has not started: `stop` and `kill` on it do nothing in Docker."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)

    def leave_now() -> None:
        if leave == "stop":
            launcher.stop(StopRequest("t1"))
        else:
            launcher.abandon(_named(launcher))

    engine.hooks[moment] = leave_now

    result = launcher.run(_run(), lambda line: None)

    assert result["stopped"] is True and result["exit"] is None
    assert _started_count(engine) == 0
    assert engine.containers == {} and engine.networks == {}


def test_a_caller_that_left_while_docker_starts_the_container_kills_it(tmp_path: Path) -> None:
    """Leaving after the start call began: the kill that found nothing running is repeated."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    engine.hooks["start"] = lambda: launcher.abandon(_named(launcher))

    result = launcher.run(_run(), lambda line: None)

    assert result["stopped"] is True
    assert sum(p.endswith("/kill") for _, p, _ in engine.calls) >= 2
    assert engine.containers == {} and engine.networks == {}


def test_a_build_whose_caller_left_while_it_waited_does_not_build(tmp_path: Path) -> None:
    """A build queued behind one of the same environment checks again on its turn."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    launcher.abandon(_named(launcher, "build", "b1"))

    result = launcher.build(_build(), lambda line: None)

    assert result["stopped"] is True and result["complete"] is False
    assert engine.created("build") == []


def test_stop_reaches_a_build_by_its_id(tmp_path: Path) -> None:
    """`stop` takes a task's or a build's id."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path, built=False)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.build(_build(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))

    assert launcher.stop(StopRequest("b1")) == {"stopped": True}
    worker.join(10)

    assert results[0]["stopped"] is True and results[0]["complete"] is False


def test_a_run_needs_a_built_environment(tmp_path: Path) -> None:
    """Docker would create a missing volume empty, filled from the image's /env."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)

    with pytest.raises(EngineError, match=r"environment .* is not built"):
        launcher.run(_run(), lambda line: None)

    assert engine.created("task") == [] and engine.networks == {}


def test_a_run_needs_the_volume_of_a_built_environment(tmp_path: Path) -> None:
    """A marker outlives `docker volume prune`: the volume must exist too."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    engine.volumes.clear()

    with pytest.raises(EngineError, match="is not built"):
        launcher.run(_run(), lambda line: None)
    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": False}


def test_a_build_replaces_an_environment_whose_volume_is_gone(tmp_path: Path) -> None:
    """A stale marker is removed and the environment is built again."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    launcher._mark_complete(environment_volume(launcher.settings, "3.13", KEY))

    result = launcher.build(_build(), lambda line: None)

    assert result["complete"] is True and len(engine.created("build")) == 1


def test_a_build_fails_when_the_old_volume_cannot_be_removed(tmp_path: Path) -> None:
    """Building into an environment a container still uses would mix two builds."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    volume = environment_volume(launcher.settings, "3.13", KEY)
    engine.containers["user"] = {
        "Name": "other",
        "Labels": {},
        "exit": None,
        "spec": {"HostConfig": {"Mounts": [{"Source": volume, "Type": "volume"}]}},
    }

    with pytest.raises(EngineError, match="still in use"):
        launcher.build(_build(), lambda line: None)

    assert engine.created("build") == [] and engine.networks == {}


def test_a_build_fails_when_docker_cannot_be_asked_to_remove_the_volume(tmp_path: Path) -> None:
    """An unreachable engine is the same refusal, not a silent build into old contents."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    _intercept(
        engine, lambda m, p, b: m == "DELETE" and p.startswith("/volumes/"), EngineError("gone")
    )

    with pytest.raises(EngineError, match="still in use"):
        launcher.build(_build(), lambda line: None)


def test_eviction_keeps_a_marker_whose_volume_cannot_be_removed(tmp_path: Path) -> None:
    """A volume in use stays, with its marker, until a later build can remove it."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False, keep_environments=1)
    old = environment_volume(launcher.settings, "3.13", KEY)
    launcher.build(_build("b1", KEY), lambda line: None)
    time.sleep(0.01)
    engine.containers["user"] = {
        "Name": "other",
        "Labels": {},
        "exit": None,
        "spec": {"HostConfig": {"Mounts": [{"Source": old, "Type": "volume"}]}},
    }

    launcher.build(_build("b2", "b" * 64), lambda line: None)

    assert (tmp_path / "environments" / f"{old}.complete").exists()
    assert old in engine.volumes


def test_handle_dispatches_each_request_kind(tmp_path: Path) -> None:
    """The socket server hands every parsed request to `handle`."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)

    assert launcher.handle(_build(), lambda line: None)["complete"] is True
    assert launcher.handle(_run(), lambda line: None)["exit"] == 0
    assert launcher.handle(StopRequest("t1"), lambda line: None) == {"stopped": False}
    assert launcher.handle(StatusRequest("3.13", KEY), lambda line: None) == {"complete": True}
    assert launcher.handle(CleanupRequest("w1"), lambda line: None) == {"removed": 0}


def test_a_failed_pull_names_the_image_and_the_next_step(tmp_path: Path) -> None:
    """A host without registry access says what to do."""
    engine = FakeEngine(images=set(), pull_works=False)

    with pytest.raises(EngineError, match=r"pull the task image ghcr\.io/chrschy/lakana-task"):
        _started(engine, tmp_path)


@pytest.mark.parametrize(
    ("match", "reply", "text"),
    [
        (
            lambda m, p, b: (
                p == "/containers/create" and b["Labels"]["lakana.launcher.kind"] == "task"
            ),
            (409, {"message": "Conflict. The container name is in use"}),
            "Docker refused the container: Conflict",
        ),
        (
            lambda m, p, b: (
                p == "/networks/create" and b["Labels"]["lakana.launcher.kind"] == "task"
            ),
            (403, {"message": "pool overlaps"}),
            "Docker refused the task network: pool overlaps",
        ),
        (
            lambda m, p, b: p.endswith("/connect"),
            (500, {"message": "no such container"}),
            "Docker could not attach server: no such container",
        ),
        (
            lambda m, p, b: p.endswith("/start") and "c2" in p,
            (500, {"message": "oci runtime error"}),
            "Docker could not start the container: oci runtime error",
        ),
    ],
)
def test_a_refused_step_is_an_engine_error_that_leaves_nothing_behind(
    tmp_path: Path, match: Any, reply: Any, text: str
) -> None:
    """Docker's own message is passed on; the network and container are removed."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, match, reply)

    with pytest.raises(EngineError, match=text):
        launcher.run(_run(), lambda line: None)

    assert engine.containers == {} and engine.networks == {}


def test_a_body_without_a_message_still_gives_a_reason(tmp_path: Path) -> None:
    """Docker's answer is not always JSON."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: p == "/networks/create", (500, b"boom"))

    with pytest.raises(EngineError, match="no message"):
        launcher.run(_run(), lambda line: None)


def test_failures_while_removing_are_logged_and_never_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The result of a run does not depend on whether its leftovers could be removed."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: m == "DELETE", EngineError("socket closed"))

    result = launcher.run(_run(), lambda line: None)

    assert result["exit"] == 0
    assert "Could not remove container" in caplog.text and "Could not remove network" in caplog.text


def test_a_stop_and_a_kill_that_docker_cannot_carry_out_are_not_errors(tmp_path: Path) -> None:
    """`stop` still says it found the task; the timeout still ends the run."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: p.endswith("/stop"), EngineError("gone"))

    result = launcher.run(_run(timeout=1), lambda line: None)

    assert result["timed_out"] is True


def test_a_kill_that_docker_cannot_carry_out_does_not_fail_the_run(tmp_path: Path) -> None:
    """The container is removed by force at the end, whatever the kill answered."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: p.endswith("/kill"), EngineError("gone"))
    engine.hooks["attach"] = lambda: launcher.abandon(_named(launcher))

    assert launcher.run(_run(), lambda line: None)["stopped"] is True


def test_a_run_ends_even_when_the_task_exits_before_reading_its_input(tmp_path: Path) -> None:
    """A task that dies at once leaves a closed pipe; that is its exit status, not an error."""
    engine = FakeEngine(exit_code=1)
    launcher = _started(engine, tmp_path)

    def closed(container_id: str, *, stdin: bool) -> socket.socket:
        ours, theirs = socket.socketpair()
        theirs.close()
        return ours

    engine.attach = closed  # ty: ignore[invalid-assignment]

    assert launcher.run(_run(), lambda line: None)["exit"] == 1


class _Chunks:
    """A stream that hands out bytes the way a socket does.

    Parameters
    ----------
    data
        What to hand out.
    """

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    def recv(self, size: int) -> bytes:
        """Return up to `size` bytes.

        Parameters
        ----------
        size
            The most to return.

        Returns
        -------
        bytes
            The next bytes; empty at the end.
        """
        return self._data.read(size)


def test_read_frames_splits_a_huge_frame_and_stops_at_a_cut_stream() -> None:
    """The size in a frame header is Docker's, but no read waits for more than a megabyte."""
    big = b"x" * (3 * 1024 * 1024 + 5)
    header = struct.pack(">BxxxI", 1, len(big))

    frames = list(read_frames(_Chunks(header + big + struct.pack(">BxxxI", 1, 10) + b"cut")))

    assert [len(f) for f in frames] == [1 << 20, 1 << 20, 1 << 20, 5]
    assert b"".join(frames) == big
    assert list(read_frames(_Chunks(b"short"))) == []


def test_an_old_container_of_this_stack_is_removed_but_a_foreign_one_is_not(tmp_path: Path) -> None:
    """Only the stack label selects."""
    engine = FakeEngine()
    engine.containers["other"] = {
        "Name": "other-task-x",
        "exit": None,
        "spec": {"HostConfig": {"Mounts": []}},
        "Labels": {"lakana.launcher.stack": "other"},
    }
    engine._released["other"] = threading.Event()

    _started(engine, tmp_path)

    assert "other" in engine.containers


def test_a_network_that_is_already_gone_is_not_an_error(tmp_path: Path) -> None:
    """A removal that finds nothing still asks Docker to delete, and moves on."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)

    launcher._close_network("lakana-task-nothing")

    assert ("DELETE", "/networks/lakana-task-nothing", None) in engine.calls


def test_a_volume_that_cannot_be_asked_about_is_not_built(tmp_path: Path) -> None:
    """An unreachable engine does not count as a built environment."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: p.startswith("/volumes/"), EngineError("gone"))

    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": False}


def test_a_run_checks_the_environment_again_after_the_pull(tmp_path: Path) -> None:
    """A prune or an eviction during a pull must not let Docker create the volume empty."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    engine.images.clear()
    engine.hooks["pull"] = engine.volumes.clear

    with pytest.raises(EngineError, match="is not built"):
        launcher.run(_run(), lambda line: None)

    assert engine.created("task") == [] and engine.networks == {}


def test_a_second_run_with_a_live_id_is_refused_and_leaves_the_first_alone(tmp_path: Path) -> None:
    """The registry is keyed by name; a duplicate must not unregister the run that owns it."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.run(_run(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))

    with pytest.raises(EngineError, match="a run with the id t1 is already live"):
        launcher.run(_run(), lambda line: None)

    assert launcher.stop(StopRequest("t1")) == {"stopped": True}
    worker.join(10)
    assert results[0]["stopped"] is True


def test_cleanup_lists_the_containers_before_it_looks_at_the_live_runs(tmp_path: Path) -> None:
    """A run that registers after the listing is spared; one that registered before is too."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    engine.containers["late"] = {
        "Name": "lakana-task-late",
        "exit": None,
        "spec": {"HostConfig": {"Mounts": []}},
        "Labels": {"lakana.launcher.stack": "lakana", "lakana.launcher.owner": "w1"},
    }
    engine._released["late"] = threading.Event()
    real = engine.request

    def listing_then_register(
        method: str, path: str, body: Any = None, *, query: Any = None, timeout: Any = 60.0
    ) -> tuple[int, Any]:
        answer = real(method, path, body, query=query, timeout=timeout)
        if path == "/containers/json":
            launcher._register("lakana-task-late", 3)
        return answer

    engine.request = listing_then_register  # ty: ignore[invalid-assignment]

    assert launcher.cleanup(CleanupRequest("w1")) == {"removed": 0}
    assert "late" in engine.containers


def test_a_host_path_in_dockers_message_is_replaced_by_the_root_name(tmp_path: Path) -> None:
    """The worker and its task log never learn where an ingestion root lives on the host."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, ingestion_roots={"data": "/srv/data"})
    _intercept(
        engine,
        lambda m, p, b: p == "/containers/create" and b["Labels"]["lakana.launcher.kind"] == "task",
        (400, {"message": "invalid mount config: bind source path does not exist: /srv/data"}),
    )

    with pytest.raises(EngineError) as caught:
        launcher.run(replace(_run(), roots=("data",)), lambda line: None)

    assert "/srv/data" not in str(caught.value) and "ingestion root 'data'" in str(caught.value)
    assert "/srv/data" not in launcher.redact("failed at /srv/data/sub")
    assert "ingestion root 'data'/sub" in launcher.redact("failed at /srv/data/sub")


def test_a_probe_failure_never_names_a_host_path(tmp_path: Path) -> None:
    """Docker's text reaches the start-up error, so it is redacted there too."""
    engine = FakeEngine()
    _intercept(
        engine,
        lambda m, p, b: (
            p == "/containers/create" and b["Labels"]["lakana.launcher.id"] == "ingest-data"
        ),
        (400, {"message": "bind source path does not exist: /srv/data"}),
    )

    assert "/srv/data" not in _reason(tmp_path, engine)


def test_any_other_answer_to_the_version_request_is_not_called_an_api_problem(
    tmp_path: Path,
) -> None:
    """A 500, or something that is not Docker, has another cause and another next step."""
    engine = FakeEngine()
    _intercept(engine, lambda m, p, b: p == "/version", (500, {"message": "daemon is starting"}))

    with pytest.raises(StartError, match="answered status 500") as caught:
        _started(engine, tmp_path)

    assert "daemon is starting" in str(caught.value)
    assert "API version" not in str(caught.value)


def test_redaction_replaces_whole_path_components_only(tmp_path: Path) -> None:
    """`/srv` is a root; `/srv2` and `/x/srv` are other places."""
    launcher = _started(
        FakeEngine(), tmp_path, ingestion_roots={"data": "/srv", "other": "/srv/other"}
    )

    redacted = launcher.redact("at /srv2/a, /x/srv/b, /srv/other/c, /srv: and /srv.")

    assert redacted == (
        "at /srv2/a, /x/srv/b, ingestion root 'other'/c, ingestion root 'data': and "
        "ingestion root 'data'."
    )


def test_an_engine_that_rejects_the_api_version_says_so_with_both_versions(tmp_path: Path) -> None:
    """A future engine may refuse /v1.47; the next step is to upgrade Lakana, not Docker."""
    engine = FakeEngine()
    _intercept(
        engine,
        lambda m, p, b: p == "/version",
        (400, {"message": "client version 1.47 is too old. Minimum supported API version is 1.52"}),
    )

    with pytest.raises(StartError, match=r"API version 1\.47 is not supported") as caught:
        _started(engine, tmp_path)

    assert "Minimum supported API version is 1.52" in str(caught.value)
    assert "upgrade Lakana" in str(caught.value)


def test_a_marker_that_disappears_during_eviction_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent eviction of another key may unlink a marker between the listing and its stat."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, keep_environments=1)
    ghost = tmp_path / "environments" / "lakana-env-ghost.complete"
    real = Path.glob
    monkeypatch.setattr(Path, "glob", lambda self, pattern: iter([ghost, *real(self, pattern)]))

    launcher._evict(keep="lakana-env-other")

    assert not ghost.exists()


def test_a_task_that_writes_before_it_reads_its_input_does_not_block_the_launcher(
    tmp_path: Path,
) -> None:
    """Input is sent while output is read, so a full pipe in one direction cannot stop the other."""
    engine = FakeEngine(write_first=True, output=[b"x" * 60000] * 100)
    launcher = _started(engine, tmp_path)
    emitted: list[dict[str, object]] = []
    big = replace(_run(), stdin=b"y" * (1 << 20))
    results: list[dict[str, object]] = []
    worker = threading.Thread(target=lambda: results.append(launcher.run(big, emitted.append)))

    worker.start()
    worker.join(15)

    assert not worker.is_alive(), "the launcher is stuck writing input while the task writes output"
    assert len(_logs(emitted)) == 60000 * 100
    assert engine.stdin == [b"y" * (1 << 20)]


def _task_started(engine: FakeEngine) -> bool:
    """Say whether the task container was started: it is supervised from then on.

    Parameters
    ----------
    engine
        The fake engine.

    Returns
    -------
    bool
        Whether a second container (the first is the start-up probe) was started.
    """
    return sum(path.endswith("/start") for _, path, _ in engine.calls) >= 2


def _until(condition: Any, seconds: float = 5.0) -> None:
    """Wait for a condition, failing after a bound.

    Parameters
    ----------
    condition
        A callable returning something truthy when done.
    seconds
        The bound.
    """
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the condition was never met"
        time.sleep(0.01)
