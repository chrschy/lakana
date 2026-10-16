"""The test process watches its own resident memory, so a runaway test cannot down the machine."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

from scripts import pytest_memory_limit as plugin
from scripts.pytest_memory_limit import (
    DEFAULT_LIMIT_MB,
    EXIT_STATUS,
    MemoryWatchdog,
    descendants,
    kill_descendants,
    open_descriptors,
    over_limit_message,
    pytest_configure,
    read_limit,
    resident_bytes,
    start_watchdog,
)

MB = 1024 * 1024
LINUX = sys.platform.startswith("linux")
ROOT = Path(__file__).resolve().parents[2]


def _status(anon_kb: int, shmem_kb: int = 0) -> str:
    return (
        "Name:\tx\nVmRSS:\t99999 kB\nRssAnon:\t"
        f"{anon_kb} kB\nRssFile:\t77777 kB\nRssShmem:\t{shmem_kb} kB\nThreads:\t1\n"
    )


def _proc(tmp_path: Path, tree: dict[int, tuple[int, int]]) -> Path:
    # A fake /proc: pid -> (parent, anonymous resident kB).
    for pid, (parent, anon_kb) in tree.items():
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "stat").write_bytes(f"{pid} (a b) c) S {parent} 1 1 0 -1\n".encode())
        (directory / "status").write_text(_status(anon_kb))
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "status").write_text(_status(1))
    (tmp_path / "uptime").write_text("1 1\n")
    return tmp_path


def test_the_default_limit_is_6_gb() -> None:
    assert read_limit({}) == DEFAULT_LIMIT_MB * MB == 6144 * MB


def test_the_environment_variable_changes_the_limit() -> None:
    assert read_limit({"LAKANA_TEST_MEMORY_LIMIT_MB": "512"}) == 512 * MB


@pytest.mark.parametrize("value", ["0", "-1"])
def test_zero_or_less_switches_it_off(value: str) -> None:
    assert read_limit({"LAKANA_TEST_MEMORY_LIMIT_MB": value}) is None


def test_a_value_that_is_not_a_number_names_the_variable() -> None:
    with pytest.raises(pytest.UsageError, match="LAKANA_TEST_MEMORY_LIMIT_MB"):
        read_limit({"LAKANA_TEST_MEMORY_LIMIT_MB": "lots"})


def test_within_the_limit_nothing_is_said() -> None:
    assert over_limit_message(100 * MB, 100 * MB, "t.py::a") is None


def test_over_the_limit_the_line_names_the_numbers_the_variable_and_the_test() -> None:
    line = over_limit_message(900 * MB, 800 * MB, "t.py::a")
    assert line is not None
    assert "900 MB" in line
    assert "800 MB" in line
    assert "LAKANA_TEST_MEMORY_LIMIT_MB" in line
    assert "t.py::a" in line
    assert f"Exit status {EXIT_STATUS}" in line


def test_the_line_is_complete_when_no_test_has_started() -> None:
    line = over_limit_message(900 * MB, 800 * MB, None)
    assert line is not None
    assert "last test started" not in line


def test_the_exit_status_is_75() -> None:
    assert EXIT_STATUS == 75  # EX_TEMPFAIL: not one of pytest's 0 to 5, not 128 plus a signal


def test_descendants_walk_the_whole_tree(tmp_path: Path) -> None:
    proc = _proc(tmp_path, {10: (1, 1), 11: (10, 1), 12: (11, 1), 13: (999, 1)})
    (proc / "99").mkdir()  # a process that vanished between listing and reading
    assert sorted(descendants(10, proc)) == [11, 12]


def test_resident_bytes_sums_anonymous_and_shared_memory_of_the_tree(tmp_path: Path) -> None:
    proc = _proc(tmp_path, {10: (1, 100), 11: (10, 50), 13: (999, 7)})
    (proc / "11" / "status").write_text(_status(50, shmem_kb=6))
    (proc / "12").mkdir()
    (proc / "12" / "stat").write_bytes(b"12 (x) S 10 1\n")  # listed, but no status any more
    (proc / "14").mkdir()
    (proc / "14" / "stat").write_bytes(b"14 (kthread) S 10 1\n")
    (proc / "14" / "status").write_text("Name:\tkthread\nThreads:\t1\n")  # no Rss lines
    assert resident_bytes(10, proc) == (100 + 50 + 6) * 1024


def test_file_backed_pages_are_not_counted(tmp_path: Path) -> None:
    proc = _proc(tmp_path, {10: (1, 100)})
    assert resident_bytes(10, proc) == 100 * 1024  # not the 99999 kB of VmRSS or 77777 of RssFile


def test_a_malformed_memory_line_counts_as_nothing(tmp_path: Path) -> None:
    proc = _proc(tmp_path, {10: (1, 100)})
    (proc / "10" / "status").write_text("RssAnon:\tlots kB\nRssShmem:\n")
    assert resident_bytes(10, proc) == 0


def test_a_process_whose_name_is_not_utf8_is_still_in_the_tree(tmp_path: Path) -> None:
    proc = _proc(tmp_path, {10: (1, 1), 11: (10, 40), 12: (11, 60)})
    (proc / "11" / "stat").write_bytes(b"11 (\xff\xfe) ) S 10 1 1 0 -1\n")
    assert sorted(descendants(10, proc)) == [11, 12]
    assert resident_bytes(10, proc) == 101 * 1024


def test_a_stat_file_that_cannot_be_read_is_skipped(tmp_path: Path) -> None:
    proc = _proc(tmp_path, {10: (1, 1)})
    (proc / "11").mkdir()
    (proc / "11" / "stat").write_text("garbage")
    assert descendants(1, proc) == [10]


def test_kill_descendants_sends_sigkill_and_ignores_the_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _proc(tmp_path, {10: (1, 1), 11: (10, 1), 12: (11, 1)})
    sent: list[tuple[int, int]] = []

    def kill(pid: int, sig: int) -> None:
        sent.append((pid, sig))
        if pid == 11:
            raise ProcessLookupError

    monkeypatch.setattr(os, "kill", kill)
    kill_descendants(10, proc)
    assert sorted(sent) == [(11, signal.SIGKILL), (12, signal.SIGKILL)]


def _watchdog(
    measures: list[int], limit: int = 100
) -> tuple[MemoryWatchdog, list[object], list[bool]]:
    events: list[object] = []
    stops = [False] * (len(measures) - 1) + [True]
    dog = MemoryWatchdog(
        limit,
        measure=lambda: measures.pop(0),
        kill=lambda: events.append("kill"),
        say=events.append,
        leave=lambda status: events.append(status),
        wait=lambda _s: stops.pop(0) if stops else True,
    )
    return dog, events, stops


def test_within_the_limit_the_watchdog_does_nothing() -> None:
    dog, events, _ = _watchdog([50, 100])
    assert dog.check_once() is False
    assert events == []


def test_over_the_limit_it_kills_the_children_then_says_why_then_exits_with_the_status() -> None:
    dog, events, _ = _watchdog([500 * MB], limit=100 * MB)
    dog.pytest_runtest_logstart("t.py::slow")
    assert dog.check_once() is True
    assert events[0] == "kill"  # a blocked stderr must not delay the kill
    assert isinstance(events[1], str)
    assert "t.py::slow" in events[1]
    assert events[2:] == [75]


def test_the_line_is_written_and_the_process_ends_when_the_kill_fails() -> None:
    events: list[object] = []

    def broken_kill() -> None:
        raise RuntimeError

    dog = MemoryWatchdog(1, lambda: 10, broken_kill, events.append, leave=events.append)
    with pytest.raises(RuntimeError):
        dog.check_once()
    assert isinstance(events[0], str)
    assert events[1:] == [75]


def test_the_process_ends_when_writing_fails() -> None:
    events: list[object] = []

    def broken_say(_m: str) -> None:
        raise OSError

    dog = MemoryWatchdog(
        1, lambda: 10, lambda: events.append("kill"), broken_say, leave=events.append
    )
    with pytest.raises(OSError):
        dog.check_once()
    assert events == ["kill", 75]


def test_the_loop_measures_until_told_to_stop_and_survives_a_failed_measurement() -> None:
    calls: list[int] = []

    def measure() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise OSError
        return 0

    stops = [False, False, True]
    dog = MemoryWatchdog(
        10,
        measure,
        lambda: None,
        lambda _m: None,
        leave=lambda _s: None,
        wait=lambda _s: stops.pop(0),
    )
    dog.run()
    assert len(calls) == 2


def test_the_thread_is_a_daemon_that_can_be_stopped() -> None:
    dog = MemoryWatchdog(10**12, lambda: 0, lambda: None, lambda _m: None, interval=0.01)
    thread = dog.start()
    assert thread.daemon
    dog.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


class _Plugins:
    def __init__(self, capture: object | None) -> None:
        self.registered: list[object] = []
        self._capture = capture

    def getplugin(self, _name: str) -> object | None:
        return self._capture

    def register(self, plug: object, _name: str) -> None:
        self.registered.append(plug)


def test_the_line_is_written_past_the_output_capture(capsys: pytest.CaptureFixture[str]) -> None:
    suspended: list[bool] = []
    capture = types.SimpleNamespace(suspend_global_capture=lambda in_: suspended.append(in_))
    config = types.SimpleNamespace(pluginmanager=_Plugins(capture))
    plugin._speaker(config)("over")  # type: ignore[arg-type]
    assert suspended == [True]
    assert capsys.readouterr().err == "over\n"


def test_the_line_is_written_when_there_is_no_capture_manager(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = types.SimpleNamespace(pluginmanager=_Plugins(None))
    plugin._speaker(config)("over")  # type: ignore[arg-type]
    assert capsys.readouterr().err == "over\n"


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin, "_started", False)


@pytest.mark.skipif(not LINUX, reason="needs /proc")
def test_start_registers_and_starts_one_watchdog(fresh: None, tmp_path: Path) -> None:
    config = types.SimpleNamespace(pluginmanager=_Plugins(None))
    dog = start_watchdog(config, {"LAKANA_TEST_MEMORY_LIMIT_MB": "1000000"})  # type: ignore[arg-type]
    assert dog is not None
    try:
        assert config.pluginmanager.registered == [dog]
        assert start_watchdog(config, {}) is None  # type: ignore[arg-type]  # one is enough
    finally:
        dog.stop()


def test_switched_off_nothing_starts(fresh: None) -> None:
    config = types.SimpleNamespace(pluginmanager=_Plugins(None))
    assert start_watchdog(config, {"LAKANA_TEST_MEMORY_LIMIT_MB": "0"}) is None  # type: ignore[arg-type]
    assert config.pluginmanager.registered == []


def test_an_xdist_worker_leaves_the_watching_to_the_controller(fresh: None) -> None:
    """A worker that exited with 75 would be replaced by xdist and the run would go on."""
    config = types.SimpleNamespace(pluginmanager=_Plugins(None))
    assert start_watchdog(config, {"PYTEST_XDIST_WORKER": "gw0"}) is None  # type: ignore[arg-type]
    assert config.pluginmanager.registered == []
    assert plugin._started is False


def test_without_proc_it_says_so_and_does_nothing(
    fresh: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = types.SimpleNamespace(pluginmanager=_Plugins(None))
    assert start_watchdog(config, {}, proc=tmp_path) is None  # type: ignore[arg-type]
    assert "not enforced" in capsys.readouterr().err
    assert config.pluginmanager.registered == []


def test_pytest_configure_uses_the_real_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKANA_TEST_MEMORY_LIMIT_MB", "0")
    pytest_configure(types.SimpleNamespace())  # type: ignore[arg-type]


def _is_gone(pid: int) -> bool:
    # A killed child is a zombie until init reaps it; a zombie is gone for this purpose.
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


@pytest.mark.skipif(not LINUX, reason="needs /proc")
def test_a_process_over_the_limit_ends_with_the_status_and_takes_its_children_along(
    tmp_path: Path,
) -> None:
    """The kill path, for real: the child holds 200 MB under a 100 MB limit, for 1 s at most."""
    program = textwrap.dedent(
        """
        import subprocess, sys, time
        from scripts.pytest_memory_limit import MemoryWatchdog, kill_descendants, resident_bytes
        import os
        child = subprocess.Popen([sys.executable, "-c",
            "import time; b = b'x' * (200 * 1024 * 1024); print('up', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE, text=True)
        assert child.stdout is not None
        child.stdout.readline()
        print(child.pid, flush=True)
        pid = os.getpid()
        dog = MemoryWatchdog(100 * 1024 * 1024, lambda: resident_bytes(pid),
            lambda: kill_descendants(pid), lambda m: sys.stderr.write(m + "\\n"),
            interval=0.05)
        dog.pytest_runtest_logstart("some::test")
        dog.start()
        time.sleep(30)
        """
    )
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert done.returncode == 75, done.stderr
    assert time.monotonic() - started < 25
    assert "over the limit of 100 MB" in done.stderr
    assert "some::test" in done.stderr
    child_pid = int(done.stdout.split()[-1])
    deadline = time.monotonic() + 5
    while not _is_gone(child_pid):
        assert time.monotonic() < deadline, "the child is still running"
        time.sleep(0.05)


def test_counting_descriptors_waits_for_a_sample_in_progress(tmp_path: Path) -> None:
    inside = threading.Event()
    release = threading.Event()

    def measure() -> int:
        descriptor = os.open(os.devnull, os.O_RDONLY)  # what a real sample holds open
        inside.set()
        release.wait(10)
        os.close(descriptor)
        return 0

    dog = MemoryWatchdog(10**12, measure, lambda: None, lambda _m: None)
    sampler = threading.Thread(target=dog.check_once)
    sampler.start()
    assert inside.wait(10)
    baseline = len(os.listdir("/proc/self/fd")) - 1  # without the sample's descriptor
    counted: list[int] = []
    counter = threading.Thread(target=lambda: counted.append(open_descriptors()))
    counter.start()
    counter.join(0.3)
    assert counter.is_alive(), "the count must wait until the sample is over"
    release.set()
    sampler.join(10)
    counter.join(10)
    assert counted == [baseline]


def test_a_kill_in_progress_also_holds_the_count_back() -> None:
    inside = threading.Event()
    release = threading.Event()

    def kill() -> None:
        inside.set()
        release.wait(10)

    dog = MemoryWatchdog(1, lambda: 10, kill, lambda _m: None, leave=lambda _s: None)
    sampler = threading.Thread(target=dog.check_once)
    sampler.start()
    assert inside.wait(10)
    counter = threading.Thread(target=open_descriptors)
    counter.start()
    counter.join(0.3)
    assert counter.is_alive()
    release.set()
    sampler.join(10)
    counter.join(10)


def test_the_real_count_is_a_number() -> None:
    assert open_descriptors() > 0


def test_the_summary_names_the_peak_and_the_limit() -> None:
    dog, _events, _ = _watchdog([60 * MB, 300 * MB, 120 * MB], limit=512 * MB)
    dog.check_once()
    dog.check_once()
    dog.check_once()
    lines: list[str] = []
    dog.pytest_terminal_summary(types.SimpleNamespace(write_line=lines.append))  # type: ignore[arg-type]
    assert lines[0] == (
        "test memory: peak 300 MB resident (pytest and child processes), limit 512 MB"
    )


def test_the_summary_warns_when_the_thread_is_not_running() -> None:
    dog = MemoryWatchdog(10**12, lambda: 0, lambda: None, lambda _m: None, interval=0.01)
    lines: list[str] = []
    reporter = types.SimpleNamespace(write_line=lines.append)
    dog.pytest_terminal_summary(reporter)  # type: ignore[arg-type]
    assert any("watchdog" in line and "stopped" in line for line in lines)
    lines.clear()
    dog.start()
    try:
        dog.pytest_terminal_summary(reporter)  # type: ignore[arg-type]
    finally:
        dog.stop()
    assert len(lines) == 1


_SESSION_CONFTEST = "from scripts.pytest_memory_limit import pytest_configure\n"
_SESSION_TEST = """
import subprocess, sys, time

def test_holds_memory(tmp_path_factory):
    child = subprocess.Popen([sys.executable, "-c",
        "import time; b = b'x' * (200 * 1024 * 1024); time.sleep(30)"])
    open("child.pid", "w").write(str(child.pid))
    time.sleep(30)
"""
_QUIET_TEST = "import time\n\ndef test_quiet():\n    time.sleep(0.7)\n"


def _session(tmp_path: Path, test: str, limit: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "conftest.py").write_text(_SESSION_CONFTEST)
    (tmp_path / "test_session.py").write_text(test)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
        env={
            **{k: v for k, v in os.environ.items() if k != "PYTEST_XDIST_WORKER"},
            "PYTHONPATH": str(ROOT),
            "LAKANA_TEST_MEMORY_LIMIT_MB": limit,
        },
    )


@pytest.mark.skipif(not LINUX, reason="needs /proc")
def test_a_real_pytest_session_over_the_limit_exits_75_and_kills_its_child(tmp_path: Path) -> None:
    """The whole chain, as the conftest files load it: plugin, thread, measure, kill, exit."""
    done = _session(tmp_path, _SESSION_TEST, "100")
    assert done.returncode == 75, done.stdout + done.stderr
    assert "over the limit of 100 MB" in done.stderr
    assert "LAKANA_TEST_MEMORY_LIMIT_MB" in done.stderr
    assert "test_session.py::test_holds_memory" in done.stderr
    child_pid = int((tmp_path / "child.pid").read_text())
    deadline = time.monotonic() + 5
    while not _is_gone(child_pid):
        assert time.monotonic() < deadline, "the child is still running"
        time.sleep(0.05)


@pytest.mark.skipif(not LINUX, reason="needs /proc")
def test_a_real_pytest_session_under_the_limit_passes_and_prints_its_peak(tmp_path: Path) -> None:
    done = _session(tmp_path, _QUIET_TEST, "1000")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "test memory: peak " in done.stdout
    assert "limit 1000 MB" in done.stdout
