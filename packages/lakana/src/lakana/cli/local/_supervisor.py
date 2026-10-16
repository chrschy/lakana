"""The local-mode process supervisor: `lakana-server` and `lakana-worker` as children."""

from __future__ import annotations

import os
import subprocess  # nosec B404 -- launches our own console scripts without a shell
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Protocol, TextIO

import httpx

_TAIL_LINES = 20
_POLL_INTERVAL = 0.2
WORKER_MEMORY_EXIT_CODE = 75
"""The exit code of a worker that stopped itself for exceeding its memory limit.
`lakana_worker.memory.EXIT_CODE` is the same number; this package does not import the worker,
and a test on each side pins the value."""
MEMORY_RESTARTS = 3
"""Restarts after a memory stop that `watch` allows within `MEMORY_RESTART_WINDOW`."""
MEMORY_RESTART_WINDOW = 300.0
"""Seconds over which memory restarts are counted; one more than `MEMORY_RESTARTS` in it ends
the run."""


class Child(Protocol):
    """A running child process, as satisfied by `subprocess.Popen`.

    `stdout` is declared as a read-only property, not a plain attribute:
    Protocol data attributes are checked invariantly, which would reject
    `subprocess.Popen[str]` (whose `stdout` is typed `IO[str] | None`) and
    any test double whose `stdout` is a narrower stream type such as
    `io.StringIO`.
    """

    pid: int

    @property
    def stdout(self) -> IO[str] | None:
        """The child's merged stdout/stderr stream, or `None` if not piped."""
        ...

    def poll(self) -> int | None:
        """Return the exit code, or `None` while the child is still running.

        Returns
        -------
        int or None
            The exit code, or `None`.
        """
        ...

    def wait(self, timeout: float | None = None) -> int:
        """Block until the child exits (or `timeout` elapses) and return its code.

        Parameters
        ----------
        timeout
            Seconds to wait before giving up.

        Returns
        -------
        int
            The exit code.

        Raises
        ------
        subprocess.TimeoutExpired
            If the child is still running when `timeout` elapses.
        """
        ...

    def terminate(self) -> None:
        """Ask the child to stop (SIGTERM on POSIX)."""
        ...

    def kill(self) -> None:
        """Force the child to stop (SIGKILL on POSIX)."""
        ...


@dataclass(frozen=True)
class SupervisorConfig:
    """Configuration for one local-mode supervision run.

    Parameters
    ----------
    server_cmd
        Argv used to launch `lakana-server`.
    worker_cmd
        Argv used to launch `lakana-worker`.
    migrate_cmd
        Argv used to run `lakana-server migrate`.
    env
        The environment passed to every child.
    health_url
        The URL `wait_ready` probes (the server's `GET /healthz`).
    ready_timeout
        Seconds to wait for a healthy probe before giving up.
    grace
        Seconds to wait after `SIGTERM` before killing a child.
    """

    server_cmd: list[str]
    worker_cmd: list[str]
    migrate_cmd: list[str]
    env: dict[str, str]
    health_url: str
    ready_timeout: float = 30.0
    grace: float = 10.0


class SupervisorError(Exception):
    """A supervised startup or readiness step failed.

    Parameters
    ----------
    message
        The error message.
    output
        The failing child's recent output, if any.
    """

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


def _spawn(cmd: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    """Launch a child process with merged, piped, text-mode stdout/stderr.

    Parameters
    ----------
    cmd
        The argv to launch.
    env
        The child's environment.

    Returns
    -------
    subprocess.Popen
        The running child. It runs in its own session (POSIX) or process
        group (Windows) so a signal delivered to this process doesn't also
        land on the child.
    """
    if os.name == "nt":  # pragma: no cover — Windows only; see plan ruling 4
        return subprocess.Popen(  # nosec B603 -- argv is our own console script without a shell
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(  # nosec B603 -- argv is our own console script without a shell
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )


def _probe(url: str) -> bool:
    """Check whether a health endpoint answers `200`.

    Parameters
    ----------
    url
        The health endpoint's URL.

    Returns
    -------
    bool
        `True` if it answered `200`; `False` on any other status or on an
        `httpx.HTTPError` (connection refused, timeout, and the like).

    Notes
    -----
    Sent with `trust_env=False`: this is always a loopback request, and
    `httpx`'s default of honouring `HTTP_PROXY`/`HTTPS_PROXY` would
    otherwise route it through a configured proxy — a dead or unreachable
    one turning a perfectly good local server into a spurious readiness
    timeout.
    """
    try:
        response = httpx.get(url, timeout=1, trust_env=False)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


class Supervisor:
    """Supervises the `lakana-server` and `lakana-worker` child processes.

    Parameters
    ----------
    config
        The commands, environment, and timing for this run.
    spawn
        Launches a child process; defaults to the real `_spawn`.
    probe
        Checks the health endpoint; defaults to the real `_probe`.
    clock
        A monotonic clock; defaults to `time.monotonic`.
    sleep
        A sleep function; defaults to `time.sleep`.
    out
        The stream child output is written to, prefixed by label.
    """

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        spawn: Callable[[list[str], dict[str, str]], Child] = _spawn,
        probe: Callable[[str], bool] = _probe,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        out: TextIO = sys.stdout,
    ) -> None:
        self._config = config
        self._spawn = spawn
        self._probe = probe
        self._clock = clock
        self._sleep = sleep
        self._out = out
        self._lock = threading.Lock()
        self._children: dict[str, Child] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._tails: dict[str, deque[str]] = {}

    def _start(self, label: str, cmd: list[str]) -> Child:
        """Spawn a child, start draining its output, and track both.

        Parameters
        ----------
        label
            The prefix used for streamed output and error tails
            (`"server"`, `"worker"`, or `"migrate"`).
        cmd
            The argv to launch.

        Returns
        -------
        Child
            The running child.
        """
        child = self._spawn(cmd, self._config.env)
        tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self._children[label] = child
        self._tails[label] = tail
        thread = threading.Thread(target=self._drain, args=(label, child, tail), daemon=True)
        thread.start()
        self._threads[label] = thread
        return child

    def _drain(self, label: str, child: Child, tail: deque[str]) -> None:
        """Stream a child's output, prefixed, keeping its tail for error messages.

        Runs on a daemon thread so a chatty child never blocks the
        supervisor while it waits or watches.

        Parameters
        ----------
        label
            The prefix written before each line.
        child
            The child whose `stdout` is drained.
        tail
            The deque collecting the child's last lines.
        """
        stream = child.stdout
        if stream is None:
            return
        try:
            for line in stream:
                tail.append(line)
                with self._lock:
                    self._out.write(f"{label} | {line}")
                    self._out.flush()
        finally:
            stream.close()

    def _join_reader(self, label: str, timeout: float = 2.0) -> None:
        """Wait for a child's reader thread, so its captured output is complete.

        Parameters
        ----------
        label
            The child's label.
        timeout
            Maximum seconds to wait.
        """
        thread = self._threads.get(label)
        if thread is not None:
            thread.join(timeout)

    def _tail_text(self, label: str) -> str:
        """Return a child's captured output tail.

        Parameters
        ----------
        label
            The child's label.

        Returns
        -------
        str
            The last lines captured for that label, joined; `""` if none.
        """
        return "".join(self._tails.get(label, ()))

    def migrate(self) -> None:
        """Run `lakana-server migrate` to completion.

        Raises
        ------
        SupervisorError
            If the migration exits non-zero, carrying its output.
        """
        child = self._start("migrate", self._config.migrate_cmd)
        code = child.wait()
        self._join_reader("migrate")
        if code != 0:
            raise SupervisorError(
                f"lakana-server migrate exited with code {code}",
                output=self._tail_text("migrate"),
            )

    def start_server(self) -> Child:
        """Launch `lakana-server` as a supervised child.

        Returns
        -------
        Child
            The running server process.
        """
        return self._start("server", self._config.server_cmd)

    def wait_ready(self, server: Child) -> None:
        """Block until the server answers healthy, exits, or times out.

        Parameters
        ----------
        server
            The server child, as returned by `start_server`.

        Raises
        ------
        SupervisorError
            If the server exits before answering healthy,
            or doesn't answer healthy within `ready_timeout` seconds — in
            which case the server is stopped before raising.
        """
        start = self._clock()
        while True:
            code = server.poll()
            if code is not None:
                self._join_reader("server")
                raise SupervisorError(
                    f"lakana-server exited with code {code} before becoming ready",
                    output=self._tail_text("server"),
                )
            if self._probe(self._config.health_url):
                return
            if self._clock() > start + self._config.ready_timeout:
                break
            self._sleep(_POLL_INTERVAL)
        self._stop_child(server)
        self._join_reader("server")
        raise SupervisorError(
            f"lakana-server didn't become ready within {self._config.ready_timeout} s"
        )

    def start_worker(self) -> Child:
        """Launch `lakana-worker` as a supervised child.

        Returns
        -------
        Child
            The running worker process.
        """
        return self._start("worker", self._config.worker_cmd)

    def watch(self) -> int:
        """Block until either child exits, then stop the other.

        A worker that exits with `WORKER_MEMORY_EXIT_CODE` stopped itself for using more than
        its memory limit: one line says so and the worker is started again, unless that
        happened more than `MEMORY_RESTARTS` times within `MEMORY_RESTART_WINDOW` seconds.

        Returns
        -------
        int
            The exiting child's exit status, treating a non-positive code
            (killed by a signal, or an unexpected clean exit — neither
            child is expected to exit on its own) as `1`; `1` as well after
            the worker has kept exceeding its memory limit.
        """
        restarts: deque[float] = deque()
        while True:
            exit_code: int | None = None
            label = ""
            while exit_code is None:
                for label in ("server", "worker"):
                    child = self._children.get(label)
                    if child is None:
                        continue
                    code = child.poll()
                    if code is not None:
                        exit_code = code
                        break
                if exit_code is None:
                    self._sleep(_POLL_INTERVAL)
            if label == "worker" and exit_code == WORKER_MEMORY_EXIT_CODE:
                now = self._clock()
                while restarts and now - restarts[0] > MEMORY_RESTART_WINDOW:
                    restarts.popleft()
                if len(restarts) < MEMORY_RESTARTS:
                    restarts.append(now)
                    self._join_reader("worker")
                    self._say(
                        "The worker used more than its memory limit and is being restarted; "
                        "what it was running has failed. The worker's start-up line says which "
                        "limit applies."
                    )
                    self.start_worker()
                    continue
                self._join_reader("worker")
                self._say(
                    f"The worker exceeded its memory limit {MEMORY_RESTARTS + 1} times within "
                    f"{MEMORY_RESTART_WINDOW / 60:.0f} minutes, so Lakana is stopping. Narrow "
                    "the query or task, or raise the limit "
                    "(for example LAKANA_WORKER__MEMORY_LIMIT=8GB) and start again."
                )
                self.stop()
                return 1
            self.stop()
            return exit_code if exit_code > 0 else 1

    def _say(self, text: str) -> None:
        """Print one line of the supervisor's own.

        Parameters
        ----------
        text
            The line.
        """
        with self._lock:
            self._out.write(f"lakana | {text}\n")
            self._out.flush()

    def _signal_terminate(self, child: Child) -> None:
        """Send this platform's graceful-stop signal to a child.

        Parameters
        ----------
        child
            The child to signal.
        """
        if os.name == "nt":  # pragma: no cover — Windows only; see plan ruling 4
            import signal

            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            child.terminate()

    def _wait_until(self, children: list[Child], deadline: float) -> None:
        """Poll a set of children until every one has exited or `deadline` passes.

        Parameters
        ----------
        children
            The children to wait for.
        deadline
            A value comparable against `clock()`; polling stops once it's
            reached, whether or not every child has exited by then.
        """
        remaining = children
        while True:
            remaining = [child for child in remaining if child.poll() is None]
            if not remaining or self._clock() >= deadline:
                return
            self._sleep(_POLL_INTERVAL)

    def _kill_stragglers(self, children: list[Child]) -> None:
        """Force-stop and reap any of these children still running.

        Parameters
        ----------
        children
            The children to check and, if needed, kill.
        """
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()

    def _stop_child(self, child: Child) -> None:
        """Stop one child: `terminate`, then `kill` after the grace period.

        Parameters
        ----------
        child
            The child to stop.
        """
        if child.poll() is None:
            self._signal_terminate(child)
            deadline = self._clock() + self._config.grace
            self._wait_until([child], deadline)
            self._kill_stragglers([child])

    def stop(self) -> None:
        """Stop every started child (idempotent; safe if never started or already exited).

        Sends the graceful-stop signal to every still-running child first,
        then waits for all of them against one shared grace deadline, then
        kills whichever are still running. This never resolves one child's
        full grace window before signalling the other, matching the design
        doc's single shared shutdown wait. Covers `"migrate"` as well as
        `"server"`/`"worker"`: a `KeyboardInterrupt` raised while `migrate()`
        blocks on `child.wait()` (Ctrl-C during migration) otherwise left
        that child running and untracked.
        """
        children = [
            child
            for label in ("server", "worker", "migrate")
            if (child := self._children.get(label)) is not None
        ]
        running = [child for child in children if child.poll() is None]
        for child in running:
            self._signal_terminate(child)
        deadline = self._clock() + self._config.grace
        self._wait_until(running, deadline)
        self._kill_stragglers(running)
        for label in ("server", "worker", "migrate"):
            self._join_reader(label)
