"""A pytest plugin: a watchdog that ends a test run that uses too much memory.

A test that loops while allocating (a fake clock that never advances, a bound removed by
a mutation) once grew to 23 GB and took the developer's machine down with it. A daemon
thread now measures the **resident** memory of the pytest process and of every process it
started (the harness, ``uv``, a server) several times a second. Over the limit it writes one
line to the real stderr, kills the descendants and ends pytest with `EXIT_STATUS`.

`RLIMIT_DATA` was tried first and replaced: it counts what a process has
*mapped*, not what it uses, and DuckDB and the allocator do not hand mappings back, so over
a suite of about 8,000 tests the count only grows. With coverage measurement it reached the
limit while about 2.7 GB were resident, and DuckDB aborted inside ``LOAD httpfs`` where it
cannot raise an error. The worker made the same change for the same reason.

Loaded for every run from ``packages/conftest.py`` and ``scripts/conftest.py``. Set
``LAKANA_TEST_MEMORY_LIMIT_MB`` to change the limit, or to ``0`` to switch it off.

Under ``pytest -n`` (pytest-xdist) only the controller starts it; it measures the workers as its
descendants, so the limit applies to the sum.

What is measured is anonymous plus shared-memory resident pages (``RssAnon`` and
``RssShmem`` of ``/proc/<pid>/status``): file-backed pages (the interpreter, libraries,
memory-mapped files) are cache the kernel can drop, and are not what a runaway test grows.

Known limits:

- A burst between two measurements is not seen (the systemd scope in ``docs/agent/testing.md``
  bounds that).
- Without ``/proc`` (not Linux) nothing is enforced.
- A process whose parent died and that init (or a subreaper) adopted is no longer linked to
  pytest, so it is neither counted nor killed. No identifier tells it from the user's other
  processes safely: the session and process group are shared with the shell, other jobs and,
  under ``systemd-run --scope``, whatever else the scope holds, and a wrong guess would kill
  something that is not part of the test run. So only parent links are followed.
- One measurement reads ``/proc/<n>/stat`` of every process on the machine (about 7 ms with
  550 processes), so its cost grows with the machine's process count: four times a second,
  well under 3 % of one core.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

VARIABLE = "LAKANA_TEST_MEMORY_LIMIT_MB"
DEFAULT_LIMIT_MB = 6144
"""The highest peak measured (the whole suite on Python 3.14 with coverage) is 3.0 GB; the two
integration files that start a server peak at 0.7 GB. This is twice that, and leaves room on
a 30 GB machine for one test job at a time. A cgroup cap of 4 or 6 GB (the scopes agents run
tests in) is at or below this limit, so inside such a scope the kernel kills first, without the
message that names the test; set the variable below the cap to get the message."""
EXIT_STATUS = 75
"""How pytest exits when it is over the limit (`EX_TEMPFAIL`). pytest itself uses 0 to 5, and a
shell reports a process killed by a signal as 128 plus the signal."""
INTERVAL_SECONDS = 0.25
"""How often the watchdog measures."""
_MB = 1024 * 1024
SAMPLING = threading.Lock()
"""Held by the watchdog for the whole of a measurement and of a kill: both keep descriptors
open on ``/proc`` for milliseconds. `open_descriptors` takes it, so a test that counts open
descriptors never sees them."""


def read_limit(environ: Mapping[str, str]) -> int | None:
    """Read the limit from the environment.

    Parameters
    ----------
    environ
        Where `LAKANA_TEST_MEMORY_LIMIT_MB` is read from.

    Returns
    -------
    int or None
        The limit in bytes; `None` when it is switched off (0 or less).

    Raises
    ------
    pytest.UsageError
        The variable is not a whole number.
    """
    raw = environ.get(VARIABLE, str(DEFAULT_LIMIT_MB))
    try:
        megabytes = int(raw)
    except ValueError:
        message = f"{VARIABLE} must be a whole number of megabytes (0 switches the limit off)."
        raise pytest.UsageError(message) from None
    return megabytes * _MB if megabytes > 0 else None


def over_limit_message(total: int, limit: int, test: str | None) -> str | None:
    """Decide whether a measurement is over the limit, and what to say about it.

    Parameters
    ----------
    total
        Resident bytes of the pytest process and its descendants.
    limit
        The limit in bytes.
    test
        The test that started last, if known.

    Returns
    -------
    str or None
        `None` when within the limit; otherwise the line to print.
    """
    if total <= limit:
        return None
    running = f"; last test started: {test}" if test else ""
    return (
        f"Test run stopped: {total // _MB} MB resident memory (pytest and its subprocesses) "
        f"is over the limit of {limit // _MB} MB{running}. "
        f"Raise the limit with {VARIABLE}=<megabytes>, or switch it off with {VARIABLE}=0. "
        f"Exit status {EXIT_STATUS}."
    )


def _stat_ids(pid: int, proc: Path) -> int | None:
    """Read a process's parent from ``/proc/<pid>/stat``.

    The file is read as bytes and split at the last ``)``, so a process name (which may hold
    any byte, and a ``)``) cannot break the parse.

    Parameters
    ----------
    pid
        The process.
    proc
        The ``/proc`` directory.

    Returns
    -------
    int or None
        The parent's pid; `None` for a process that has just exited.
    """
    try:
        data = (proc / str(pid) / "stat").read_bytes()
        return int(data.rsplit(b")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def descendants(root: int, proc: Path = Path("/proc")) -> list[int]:
    """List every live descendant of a process (Linux).

    Parameters
    ----------
    root
        The process.
    proc
        The ``/proc`` directory.

    Returns
    -------
    list of int
        The pids of the children, grandchildren and so on.
    """
    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if entry.name.isdigit():
            parent = _stat_ids(int(entry.name), proc)
            if parent is not None:
                children.setdefault(parent, []).append(int(entry.name))
    found: list[int] = []
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


def _resident(pid: int, proc: Path) -> int:
    """Read one process's anonymous and shared-memory resident bytes.

    Parameters
    ----------
    pid
        The process.
    proc
        The ``/proc`` directory.

    Returns
    -------
    int
        ``RssAnon`` plus ``RssShmem`` of ``/proc/<pid>/status`` in bytes; 0 for a process that
        has just exited and for a kernel thread, which has neither line.
    """
    try:
        data = (proc / str(pid) / "status").read_bytes()
    except OSError:
        return 0
    total = 0
    for line in data.splitlines():
        if line.startswith((b"RssAnon:", b"RssShmem:")):
            try:
                total += int(line.split()[1]) * 1024
            except (ValueError, IndexError):
                continue
    return total


def resident_bytes(root: int, proc: Path = Path("/proc")) -> int:
    """Measure the resident memory of a process and all its live descendants (Linux).

    Parameters
    ----------
    root
        The process.
    proc
        The ``/proc`` directory.

    Returns
    -------
    int
        Anonymous plus shared-memory resident bytes of the tree.
    """
    return sum(_resident(p, proc) for p in [root, *descendants(root, proc)])


def kill_descendants(root: int, proc: Path = Path("/proc")) -> None:
    """Send SIGKILL to every live descendant of a process.

    Parameters
    ----------
    root
        The process.
    proc
        The ``/proc`` directory.
    """
    for pid in descendants(root, proc):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue  # already gone


class MemoryWatchdog:
    """Measure once per interval and end the process when it is over the limit.

    Parameters
    ----------
    limit
        The limit in bytes.
    measure
        Returns the current resident bytes.
    kill
        Kills the descendants.
    say
        Writes the line to the real stderr.
    leave
        Ends the process with a status; it must not return (a test's may).
    interval
        Seconds between measurements.
    wait
        Waits `interval` seconds and returns `True` to stop (a seam for tests).
    """

    def __init__(
        self,
        limit: int,
        measure: Callable[[], int],
        kill: Callable[[], None],
        say: Callable[[str], None],
        leave: Callable[[int], None] = os._exit,
        interval: float = INTERVAL_SECONDS,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._limit = limit
        self._measure = measure
        self._kill = kill
        self._say = say
        self._leave = leave
        self._interval = interval
        self._stop = threading.Event()
        self._wait = wait if wait is not None else self._stop.wait
        self.test: str | None = None
        self.peak = 0
        self.thread: threading.Thread | None = None

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        """Remember the test that started last (a pytest hook).

        Parameters
        ----------
        nodeid
            The test's id.
        """
        self.test = nodeid

    def check_once(self) -> bool:
        """Measure and act once.

        Over the limit the children are killed first (a blocked stderr must not delay that),
        then the line is written, then the process ends.

        Returns
        -------
        bool
            Whether the limit was exceeded (only a test's `leave` lets this return).
        """
        with SAMPLING:
            total = self._measure()
        self.peak = max(self.peak, total)
        message = over_limit_message(total, self._limit, self.test)
        if message is None:
            return False
        try:
            with SAMPLING:
                self._kill()
        finally:
            try:
                self._say(message)
            finally:
                self._leave(EXIT_STATUS)
        return True

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        """Print the peak seen, and a warning if the thread is not running (a pytest hook).

        Parameters
        ----------
        terminalreporter
            Where the line goes.
        """
        terminalreporter.write_line(
            f"test memory: peak {self.peak // _MB} MB resident (pytest and child processes), "
            f"limit {self._limit // _MB} MB"
        )
        if self.thread is None or not self.thread.is_alive():
            terminalreporter.write_line(
                "warning: the test memory watchdog thread stopped, so the limit was not "
                "enforced for the rest of the run."
            )

    def run(self) -> None:
        """Loop until stopped. A failed measurement is skipped; the next one tries again."""
        while not self._wait(self._interval):
            try:
                self.check_once()
            except Exception:
                continue

    def start(self) -> threading.Thread:
        """Start the loop in a daemon thread, which does not keep pytest alive at exit.

        Returns
        -------
        threading.Thread
            The thread.
        """
        thread = threading.Thread(target=self.run, name="pytest-memory-watchdog", daemon=True)
        thread.start()
        self.thread = thread
        return thread

    def stop(self) -> None:
        """Ask the loop to end."""
        self._stop.set()


def open_descriptors(directory: Path = Path("/proc/self/fd")) -> int:
    """Count this process's open file descriptors, never during a watchdog sample.

    The watchdog keeps a few files and directories open on ``/proc`` for milliseconds, four
    times a second. A test that compares the count before and after the code it checks would
    fail now and then on that; this waits for a sample in progress to end (`SAMPLING`).

    Parameters
    ----------
    directory
        The directory listing the descriptors.

    Returns
    -------
    int
        The number of open descriptors.
    """
    with SAMPLING:
        return len(os.listdir(directory))


def _speaker(config: pytest.Config) -> Callable[[str], None]:
    """Build the function that writes to the real stderr, past pytest's output capture.

    Parameters
    ----------
    config
        The pytest configuration, which knows the capture manager.

    Returns
    -------
    Callable[[str], None]
        Suspends the capture, writes the line, and does not resume it: the process ends next.
    """

    def say(message: str) -> None:
        manager = config.pluginmanager.getplugin("capturemanager")
        if manager is not None:
            manager.suspend_global_capture(in_=True)
        sys.stderr.write(message + "\n")
        sys.stderr.flush()

    return say


_started = False


def start_watchdog(
    config: pytest.Config,
    environ: Mapping[str, str],
    proc: Path = Path("/proc"),
) -> MemoryWatchdog | None:
    """Start the watchdog unless it is switched off, already running, or `/proc` is missing.

    Under ``pytest -n`` (pytest-xdist) only the controller watches: it measures all its
    descendants, the workers among them, so the total stays bounded. A worker that ended
    itself with `EXIT_STATUS` would only be replaced by xdist and the run would go on, so a
    worker (``PYTEST_XDIST_WORKER`` is set) starts none.

    Parameters
    ----------
    config
        The pytest configuration; the watchdog is registered as a plugin for its hooks.
    environ
        Where `LAKANA_TEST_MEMORY_LIMIT_MB` is read from.
    proc
        The ``/proc`` directory.

    Returns
    -------
    MemoryWatchdog or None
        The running watchdog; `None` when none was started.
    """
    global _started
    limit = read_limit(environ)
    if limit is None or _started or "PYTEST_XDIST_WORKER" in environ:
        return None
    if not (proc / "self" / "status").exists():
        sys.stderr.write(f"{VARIABLE}: no /proc here, so the test memory limit is not enforced.\n")
        return None
    _started = True
    pid = os.getpid()
    watchdog = MemoryWatchdog(
        limit,
        measure=lambda: resident_bytes(pid, proc),
        kill=lambda: kill_descendants(pid, proc),
        say=_speaker(config),
    )
    config.pluginmanager.register(watchdog, "lakana-memory-watchdog")
    watchdog.start()
    return watchdog


def pytest_configure(config: pytest.Config) -> None:
    """Start the watchdog before any test is collected.

    Parameters
    ----------
    config
        The pytest configuration.
    """
    start_watchdog(config, os.environ)
