"""The launcher's operations against Docker: run, build, stop, status and cleanup.

`Launcher` creates a network per task or build (internal, no gateway on the host), a container
from the specs of `lakana_worker.launcher.requests`, streams the container's output to the
caller, and removes both at the end, on a stop, on the timeout, and when the caller's connection
closes. At start it checks the engine, finds out whether `no-new-privileges` works, and removes
every container and network that carries its stack label: their owners run them again.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from lakana_worker.launcher.engine import API_VERSION, Engine, EngineError
from lakana_worker.launcher.requests import (
    LABEL_OWNER,
    LABEL_STACK,
    BuildRequest,
    CleanupRequest,
    Request,
    RunRequest,
    StatusRequest,
    StopRequest,
    build_container_spec,
    container_name,
    environment_volume,
    ingestion_probe_spec,
    network_spec,
    probe_container_spec,
    run_container_spec,
)
from lakana_worker.launcher.settings import LauncherSettings

logger = logging.getLogger("lakana_worker.launcher")

MIN_ENGINE_MAJOR = 28
"""The first Docker Engine with `gateway_mode_ipv4=isolated`."""

Emit = Callable[[dict[str, object]], None]
"""Sends one answer line to the caller; raises `OSError` when the caller is gone."""

_STOPPED_BEFORE_START: dict[str, object] = {
    "exit": None,
    "oom_killed": False,
    "timed_out": False,
    "stopped": True,
}
"""The result of a run whose caller left, or that was stopped, before its container started."""
_PULL_SECONDS = 600.0
_PROBE_SECONDS = 300.0
_MAX_FRAME_PIECE = 1 << 20
"""The most one read of a container's output takes, whatever size a frame header claims."""
_SPECIAL_MOUNT = 3
_SOCKET_FOUND = 4
_TOO_MANY = 5
_UNREADABLE = 6
"""Exit statuses of the ingestion-root probe."""


class StartError(Exception):
    """The launcher cannot run on this Docker; the text says what to change."""


@dataclass
class _Live:
    """A run or build the launcher supervises, from its registration to its end.

    Parameters
    ----------
    grace
        Seconds between SIGTERM and SIGKILL on a stop.
    container_id
        Docker's id, once the container exists.
    stopped
        Set by `stop` and `abandon`; checked before every step that cannot be undone.
    timed_out
        Set by the timer.
    """

    grace: int
    container_id: str | None = None
    stopped: threading.Event = field(default_factory=threading.Event)
    timed_out: threading.Event = field(default_factory=threading.Event)


def _message(body: object) -> str:
    """Pick Docker's own message out of an error body, bounded.

    Parameters
    ----------
    body
        The decoded body.

    Returns
    -------
    str
        At most 300 characters.
    """
    text = body.get("message") if isinstance(body, dict) else None
    return str(text if text else "no message")[:300]


class _Receiver(Protocol):
    """Anything that hands out bytes the way a socket does."""

    def recv(self, size: int, /) -> bytes:
        """Return up to `size` bytes, or nothing at the end.

        Parameters
        ----------
        size
            The most to return.

        Returns
        -------
        bytes
            The next bytes.
        """
        ...


def _send_input(stream: socket.socket, data: bytes) -> None:
    """Write a task's input and close the write side.

    A task that exited at once leaves nothing to read its input: its exit status says so, so a
    broken pipe is not an error here. Closing the stream ends the write too.

    Parameters
    ----------
    stream
        The attached stream.
    data
        The input.
    """
    with contextlib.suppress(OSError):
        stream.sendall(data)
        stream.shutdown(socket.SHUT_WR)


def _mtime(path: Path) -> float:
    """Read a file's modification time, 0 when it is gone.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    float
        Seconds since the epoch; 0 for a file removed meanwhile (the oldest).
    """
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _exact(stream: _Receiver, size: int) -> bytes | None:
    """Read exactly `size` bytes, or `None` at the end of the stream.

    Parameters
    ----------
    stream
        The attached stream.
    size
        How many bytes.

    Returns
    -------
    bytes or None
        The bytes.
    """
    data = b""
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_frames(stream: _Receiver) -> Iterator[bytes]:
    """Yield the payloads of Docker's multiplexed stdout and stderr frames.

    Parameters
    ----------
    stream
        The attached stream.

    Yields
    ------
    bytes
        A frame's payload, in pieces of at most 1 MiB: the size in a header is not trusted with
        one allocation.
    """
    while (header := _exact(stream, 8)) is not None:
        remaining = struct.unpack(">I", header[4:])[0]
        while remaining:
            payload = _exact(stream, min(remaining, _MAX_FRAME_PIECE))
            if payload is None:
                return
            remaining -= len(payload)
            yield payload


class Launcher:
    """Runs tasks and builds in containers, from parsed requests.

    Parameters
    ----------
    settings
        The allow-lists, ceilings and names.
    engine
        The Docker Engine client.
    """

    def __init__(self, settings: LauncherSettings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.no_new_privileges = settings.no_new_privileges == "on"
        self._live: dict[str, _Live] = {}
        self._abandoned: set[str] = set()
        self._lock = threading.Lock()
        self._build_locks: dict[str, threading.Lock] = {}

    # --- start-up -----------------------------------------------------------------------

    def start(self) -> None:
        """Check the engine and the ingestion roots, clear what an earlier launcher left.

        Raises
        ------
        StartError
            If the engine is older than `MIN_ENGINE_MAJOR` or cannot make an isolated network,
            the option is ``on`` and does not work here, or an ingestion root fails its probe.
        EngineError
            If Docker cannot be reached.
        """
        status, version = self.engine.request("GET", "/version")
        text = str(version.get("Version", "")) if isinstance(version, dict) else ""
        head = text.split(".")[0]
        if status != 200:
            raise StartError(
                f"Docker does not accept API version {API_VERSION.removeprefix('/v')}, the one "
                f"this release of Lakana speaks (Docker Engine {MIN_ENGINE_MAJOR} or later): "
                f"{self.redact(_message(version))}. API version 1.47 is not supported by this "
                "Docker; upgrade Lakana to a release that supports it, or use a Docker Engine "
                "that still serves this API version."
            )
        if not head.isdigit() or int(head) < MIN_ENGINE_MAJOR:
            raise StartError(
                f"Docker Engine {MIN_ENGINE_MAJOR} or later is needed: it can keep a task's "
                "network away from the host (gateway_mode_ipv4=isolated). This engine reports "
                f"'{text or 'no version'}'; upgrade Docker."
            )
        removed = self._remove_labelled({LABEL_STACK: self.settings.stack}, spare_live=False)
        if removed:
            logger.info(
                "Removed %d containers an earlier launcher left; their workers run them again.",
                removed,
            )
        self._check_isolated_network(text)
        works = self._probe_no_new_privileges()
        if self.settings.no_new_privileges == "on" and not works:
            raise StartError(
                "LAKANA_LAUNCHER__NO_NEW_PRIVILEGES is on, but this Docker cannot start a "
                "container with no-new-privileges (Docker installed as a snap refuses every "
                "one). Set it to auto, which leaves the option off where it breaks."
            )
        self.no_new_privileges = works
        logger.info(
            "no-new-privileges is %s for task and build containers.",
            "on" if works else "off: this Docker refuses it",
        )
        (self.settings.state_dir / "environments").mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_ingestion_roots()

    def _check_isolated_network(self, version: str) -> None:
        """Make an isolated network and see that Docker kept the mode, then remove it.

        An engine that does not know the mode may accept the network and drop the option; the
        task would then reach the host's services through the network's gateway.

        Parameters
        ----------
        version
            The engine's version, for the message.

        Raises
        ------
        StartError
            If the network cannot be made, or Docker did not keep the option.
        """
        spec = network_spec(self.settings, "probe", "isolated-check", "launcher")
        name = str(spec["Name"])
        status, body = self.engine.request("POST", "/networks/create", spec)
        if status != 201:
            raise StartError(
                "Docker cannot create an isolated network "
                f"(gateway_mode_ipv4=isolated): {_message(body)}. Docker Engine {version} is "
                f"running; Docker Engine {MIN_ENGINE_MAJOR} or later is needed."
            )
        try:
            _, info = self.engine.request("GET", f"/networks/{quote(name, safe='')}")
            options = info.get("Options") if isinstance(info, dict) else None
            if not isinstance(options, dict) or (
                options.get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
            ):
                raise StartError(
                    "Docker created a network but did not keep gateway_mode_ipv4=isolated, so a "
                    f"task could reach the host through its gateway. Docker Engine {version} is "
                    f"running; Docker Engine {MIN_ENGINE_MAJOR} or later is needed."
                )
        finally:
            self._close_network(name)

    def _run_probe(self, name: str, spec: dict[str, Any]) -> tuple[int | None, str]:
        """Create, start and wait for a probe container, then remove it.

        Parameters
        ----------
        name
            The probe's name.
        spec
            Its spec.

        Returns
        -------
        tuple
            The container's exit status (`None` when it did not start or Docker gave none) and
            Docker's message when it did not start (empty otherwise).

        Raises
        ------
        EngineError
            If Docker refuses the container or cannot be reached.
        """
        with contextlib.suppress(EngineError):
            self.engine.request(
                "DELETE", f"/containers/{quote(name, safe='')}", query={"force": "1"}
            )
        cid = self._create(name, spec)
        try:
            status, body = self.engine.request("POST", f"/containers/{cid}/start")
            if status != 204:
                return None, _message(body)
            _, waited = self.engine.request(
                "POST", f"/containers/{cid}/wait", timeout=_PROBE_SECONDS
            )
            code = waited.get("StatusCode") if isinstance(waited, dict) else None
            return (code if isinstance(code, int) else None), ""
        finally:
            self._remove_container(cid)

    def _probe_image(self) -> str:
        """Pick the image the probes run in, pulling it if the host has not got it.

        Returns
        -------
        str
            The reference.
        """
        image = self.settings.images[min(self.settings.images)]
        self._ensure_image(image)
        return image

    def _probe_no_new_privileges(self) -> bool:
        """Start ``python -c pass`` with the option and see whether it exits 0.

        Returns
        -------
        bool
            Whether the option works on this Docker.
        """
        image = self._probe_image()
        name = container_name(self.settings, "probe", "no-new-privileges")
        code, _ = self._run_probe(name, probe_container_spec(self.settings, image))
        return code == 0

    def _check_ingestion_roots(self) -> None:
        """Look at every allowed ingestion root from inside a container, as a task would.

        Raises
        ------
        StartError
            If a root is, or holds, a proc, sysfs or devtmpfs mount or a socket, or cannot be
            checked. The message names the root, never its host path.
        """
        for root in sorted(self.settings.ingestion_roots):
            image = self._probe_image()
            name = container_name(self.settings, "probe", f"ingest-{root}")
            spec = ingestion_probe_spec(
                self.settings, image, root, no_new_privileges=self.no_new_privileges
            )
            try:
                code, refusal = self._run_probe(name, spec)
            except EngineError as exc:
                raise StartError(
                    f"cannot check ingestion root '{root}': {self.redact(str(exc))}; check that "
                    "its directory exists on the host (LAKANA_LAUNCHER__INGESTION_ROOTS)"
                ) from None
            if refusal:
                raise StartError(
                    f"cannot check ingestion root '{root}': Docker could not start the probe: "
                    f"{self.redact(refusal)}; check that its directory exists on the host "
                    "(LAKANA_LAUNCHER__INGESTION_ROOTS)"
                )
            if code == _SPECIAL_MOUNT:
                raise StartError(
                    f"ingestion root '{root}' is, or holds, a proc, sysfs or devtmpfs mount; a "
                    "task would see the host through it. Choose a plain data directory "
                    "(LAKANA_LAUNCHER__INGESTION_ROOTS), not a link to one."
                )
            if code == _SOCKET_FOUND:
                raise StartError(
                    f"ingestion root '{root}' holds a socket; a read-only mount does not stop a "
                    "task from connecting to it. Move the socket out of the directory or choose "
                    "another one (LAKANA_LAUNCHER__INGESTION_ROOTS)."
                )
            if code == _TOO_MANY:
                raise StartError(
                    f"ingestion root '{root}' has more than 100000 entries, so it cannot be "
                    "checked for sockets. Point the root at a smaller directory "
                    "(LAKANA_LAUNCHER__INGESTION_ROOTS)."
                )
            if code == _UNREADABLE:
                raise StartError(
                    f"ingestion root '{root}' holds something the task user cannot list or "
                    "examine, so it cannot be checked for sockets. Make every directory in it "
                    "readable by the task user (10001), or choose another directory "
                    "(LAKANA_LAUNCHER__INGESTION_ROOTS)."
                )
            if code != 0:
                raise StartError(
                    f"ingestion root '{root}' could not be checked (the probe ended with "
                    f"{code if code is not None else 'no exit status'}); check its permissions "
                    "and `docker compose logs launcher`"
                )

    # --- operations ---------------------------------------------------------------------

    def handle(self, request: Request, emit: Emit) -> dict[str, object]:
        """Run one parsed request.

        Parameters
        ----------
        request
            The request.
        emit
            Sends log lines to the caller.

        Returns
        -------
        dict
            The result line's value.
        """
        if isinstance(request, RunRequest):
            return self.run(request, emit)
        if isinstance(request, BuildRequest):
            return self.build(request, emit)
        if isinstance(request, StopRequest):
            return self.stop(request)
        if isinstance(request, StatusRequest):
            return self.status(request)
        return self.cleanup(request)

    def run(self, request: RunRequest, emit: Emit) -> dict[str, object]:
        """Run one task attempt to its end.

        Parameters
        ----------
        request
            The run.
        emit
            Sends ``{"log": <base64>}`` lines.

        Returns
        -------
        dict
            ``exit``, ``oom_killed``, ``timed_out``, ``stopped``.

        Raises
        ------
        EngineError
            If Docker refuses a step or the environment is not built; the container and network
            are removed first.
        Exception
            Whatever `emit` raises when the caller is gone; likewise.
        """
        name = container_name(self.settings, "task", request.task)
        live = self._register(name, request.kill_grace_seconds)
        try:
            volume = environment_volume(self.settings, request.python, request.environment)
            if not self._built(volume):
                raise self._not_built(request.python, request.environment)
            self._touch(volume)
            if live.stopped.is_set():
                return dict(_STOPPED_BEFORE_START)
            network = self._open_network(
                "task",
                request.task,
                request.owner,
                (self.settings.server_service, self.settings.server_alias),
            )
            try:
                spec = run_container_spec(
                    request, self.settings, no_new_privileges=self.no_new_privileges
                )
                self._ensure_image(spec["Image"])
                # A pull takes minutes; a prune or an eviction in that time must not let Docker
                # create the volume empty at the mount.
                if not self._built(volume):
                    raise self._not_built(request.python, request.environment)
                return self._supervise(
                    name, spec, live, request.limits.timeout_seconds, emit, stdin=request.stdin
                )
            finally:
                self._close_network(network)
        finally:
            self.forget(name, live)

    @staticmethod
    def _not_built(python: str, environment: str) -> EngineError:
        """Describe a run for an environment that is not built.

        Parameters
        ----------
        python
            The Python version.
        environment
            The environment key.

        Returns
        -------
        EngineError
            The error to raise.
        """
        return EngineError(
            f"the environment {environment[:12]} for Python {python} is not built (it may have "
            "been removed); build it first"
        )

    def build(self, request: BuildRequest, emit: Emit) -> dict[str, object]:
        """Build one environment, unless it is built already; one build per environment at once.

        Parameters
        ----------
        request
            The build.
        emit
            Sends the build's output.

        Returns
        -------
        dict
            ``complete`` and, when a build ran, its ``exit``, ``oom_killed``, ``timed_out`` and
            ``stopped``.

        Raises
        ------
        EngineError
            If Docker refuses a step, or the volume of an earlier attempt cannot be removed.
        Exception
            Whatever `emit` raises when the caller is gone.
        """
        name = container_name(self.settings, "build", request.build)
        live = self._register(name, 0)
        try:
            volume = environment_volume(self.settings, request.python, request.environment)
            with self._lock:
                lock = self._build_locks.setdefault(volume, threading.Lock())
            with lock:
                return self._build_locked(request, emit, name, live, volume)
        finally:
            self.forget(name, live)

    def _build_locked(
        self, request: BuildRequest, emit: Emit, name: str, live: _Live, volume: str
    ) -> dict[str, object]:
        """Build one environment, holding its lock.

        Parameters
        ----------
        request
            The build.
        emit
            Sends the build's output.
        name
            The build container's name.
        live
            The build's registration.
        volume
            The environment volume.

        Returns
        -------
        dict
            As `build`.

        Raises
        ------
        EngineError
            If Docker refuses a step, or an old volume cannot be removed.
        """
        if live.stopped.is_set():
            return {**_STOPPED_BEFORE_START, "complete": False}
        if self._built(volume):
            self._touch(volume)
            return {
                "complete": True,
                "exit": 0,
                "oom_killed": False,
                "timed_out": False,
                "stopped": False,
            }
        self._marker(volume).unlink(missing_ok=True)
        if not self._remove_volume(volume):
            raise EngineError(
                f"the volume of environment {request.environment[:12]} is still in use or cannot "
                "be removed; wait for the tasks that use it, or check "
                "`docker compose logs launcher`"
            )
        network = self._open_network("build", request.build, request.owner, None)
        try:
            spec = build_container_spec(
                request, self.settings, no_new_privileges=self.no_new_privileges, proxy_url=None
            )
            self._ensure_image(spec["Image"])
            result = self._supervise(name, spec, live, request.limits.timeout_seconds, emit)
        finally:
            self._close_network(network)
        complete = (
            result["exit"] == 0
            and not result["oom_killed"]
            and not result["timed_out"]
            and not result["stopped"]
        )
        if complete:
            self._mark_complete(volume)
            self._evict(keep=volume)
        else:
            self._remove_volume(volume)
        return {**result, "complete": complete}

    def stop(self, request: StopRequest) -> dict[str, object]:
        """Stop a running task or build: SIGTERM, SIGKILL after its grace.

        Parameters
        ----------
        request
            The id.

        Returns
        -------
        dict
            ``stopped``: whether something was registered under that id. A run that has not
            started its container yet never will.
        """
        for kind in ("task", "build"):
            with self._lock:
                live = self._live.get(container_name(self.settings, kind, request.id))
            if live is not None:
                live.stopped.set()
                cid = live.container_id
                if cid is not None:
                    with contextlib.suppress(EngineError):
                        self.engine.request(
                            "POST",
                            f"/containers/{cid}/stop",
                            query={"t": str(live.grace)},
                            timeout=live.grace + 30.0,
                        )
                return {"stopped": True}
        return {"stopped": False}

    def abandon(self, name: str) -> None:
        """Kill a run at once and never start it: its caller's connection closed.

        The name is remembered, because the connection can close before the request reaches
        `run` or `build`, which looks the name up when it registers.

        Parameters
        ----------
        name
            The container's name.
        """
        with self._lock:
            self._abandoned.add(name)
            live = self._live.get(name)
        if live is None:
            return
        live.stopped.set()
        self._kill(live)

    def forget(self, name: str, live: _Live | None = None) -> None:
        """Drop a finished run: its registration and any abandonment of its name.

        Parameters
        ----------
        name
            The container's name.
        live
            The registration to drop. Another run's registration under the same name is left
            alone; `None` (the socket server's call, after the run unregistered itself) drops
            only the abandonment.
        """
        with self._lock:
            if live is not None and self._live.get(name) is live:
                del self._live[name]
            self._abandoned.discard(name)

    def status(self, request: StatusRequest) -> dict[str, object]:
        """Say whether an environment is built.

        Parameters
        ----------
        request
            Python and key.

        Returns
        -------
        dict
            ``complete``: the marker and the volume both exist.
        """
        volume = environment_volume(self.settings, request.python, request.environment)
        complete = self._built(volume)
        if complete:
            self._touch(volume)
        return {"complete": complete}

    def cleanup(self, request: CleanupRequest) -> dict[str, object]:
        """Remove a worker's containers that no live connection supervises.

        Parameters
        ----------
        request
            The worker.

        Returns
        -------
        dict
            ``removed``: how many containers.
        """
        removed = self._remove_labelled(
            {LABEL_STACK: self.settings.stack, LABEL_OWNER: request.owner}, spare_live=True
        )
        return {"removed": removed}

    def _register(self, name: str, grace: int) -> _Live:
        """Register a run or build, stopped at once when its caller left already.

        Parameters
        ----------
        name
            The container's name.
        grace
            Seconds between SIGTERM and SIGKILL.

        Returns
        -------
        _Live
            The registration.

        Raises
        ------
        EngineError
            If a run is registered under that name already.
        """
        live = _Live(grace)
        with self._lock:
            if name in self._live:
                ident = name.removeprefix(f"{self.settings.stack}-").split("-", 1)[-1]
                raise EngineError(
                    f"a run with the id {ident} is already live; wait for it to end or use "
                    "another id"
                )
            self._live[name] = live
            if name in self._abandoned:
                live.stopped.set()
        return live

    def redact(self, text: str) -> str:
        """Replace the host path of every ingestion root in a text by the root's name.

        Parameters
        ----------
        text
            A message, such as Docker's.

        Returns
        -------
        str
            The text without any root's host path.
        """
        for name, path in sorted(
            self.settings.ingestion_roots.items(), key=lambda item: -len(item[1])
        ):
            text = text.replace(path, f"ingestion root '{name}'")
        return text

    def _kill(self, live: _Live) -> None:
        """Kill a container if it exists; a container that has not started ignores it.

        Parameters
        ----------
        live
            The registration.
        """
        if live.container_id is not None:
            with contextlib.suppress(EngineError):
                self.engine.request("POST", f"/containers/{live.container_id}/kill")

    # --- containers ---------------------------------------------------------------------

    def _supervise(
        self,
        name: str,
        spec: dict[str, Any],
        live: _Live,
        timeout: int,
        emit: Emit,
        *,
        stdin: bytes | None = None,
    ) -> dict[str, object]:
        """Create, attach, start, stream and remove one container.

        The registration is checked after the create, before the start and after the start: a
        stop or a closed connection in any of those windows finds a container that ignores a
        stop or a kill, so the step after it must not go on.

        Parameters
        ----------
        name
            Its name.
        spec
            Its spec.
        live
            Its registration.
        timeout
            Seconds before it is stopped.
        emit
            Sends its output.
        stdin
            Bytes for its stdin, or `None`.

        Returns
        -------
        dict
            ``exit``, ``oom_killed``, ``timed_out``, ``stopped``.

        Raises
        ------
        EngineError
            If Docker refuses a step.
        """
        if live.stopped.is_set():
            return dict(_STOPPED_BEFORE_START)
        cid = live.container_id = self._create(name, spec)
        timer = threading.Timer(timeout, self._expire, (live,))
        timer.daemon = True
        try:
            if live.stopped.is_set():
                return dict(_STOPPED_BEFORE_START)
            stream = self.engine.attach(cid, stdin=stdin is not None)
            try:
                if live.stopped.is_set():
                    return dict(_STOPPED_BEFORE_START)
                status, body = self.engine.request("POST", f"/containers/{cid}/start")
                if status != 204:
                    raise EngineError(f"Docker could not start the container: {_message(body)}")
                if live.stopped.is_set():
                    self._kill(live)
                timer.start()
                if stdin is not None:
                    # Input goes in while output is read: a task that writes before it reads
                    # fills one pipe while the launcher fills the other.
                    threading.Thread(target=_send_input, args=(stream, stdin), daemon=True).start()
                for payload in read_frames(stream):
                    emit({"log": base64.b64encode(payload).decode("ascii")})
            finally:
                stream.close()
            _, waited = self.engine.request("POST", f"/containers/{cid}/wait", timeout=None)
            _, info = self.engine.request("GET", f"/containers/{cid}/json")
            state = info.get("State", {}) if isinstance(info, dict) else {}
            code = waited.get("StatusCode") if isinstance(waited, dict) else None
            return {
                "exit": code if isinstance(code, int) else None,
                "oom_killed": bool(state.get("OOMKilled")),
                "timed_out": live.timed_out.is_set(),
                "stopped": live.stopped.is_set(),
            }
        finally:
            timer.cancel()
            self._remove_container(cid)

    def _expire(self, live: _Live) -> None:
        """Stop a container whose timeout passed.

        Parameters
        ----------
        live
            The container.
        """
        live.timed_out.set()
        with contextlib.suppress(EngineError):
            self.engine.request(
                "POST",
                f"/containers/{live.container_id}/stop",
                query={"t": str(live.grace)},
                timeout=live.grace + 30.0,
            )

    def _create(self, name: str, spec: dict[str, Any]) -> str:
        """Create a container.

        Parameters
        ----------
        name
            Its name.
        spec
            Its spec.

        Returns
        -------
        str
            Its id.

        Raises
        ------
        EngineError
            If Docker refuses it, with Docker's message (a subpath escape is refused here).
        """
        status, body = self.engine.request("POST", "/containers/create", spec, query={"name": name})
        if status != 201 or not isinstance(body, dict):
            raise EngineError(f"Docker refused the container: {self.redact(_message(body))}")
        return str(body["Id"])

    def _remove_container(self, cid: str) -> None:
        """Remove a container and kill what still runs in it.

        Parameters
        ----------
        cid
            Its id.
        """
        try:
            self.engine.request(
                "DELETE", f"/containers/{quote(cid, safe='')}", query={"force": "1"}
            )
        except EngineError as exc:
            logger.error("Could not remove container %s: %s", cid, exc)

    def _ensure_image(self, image: str) -> None:
        """Pull an allowed image once if it is not on the host.

        Parameters
        ----------
        image
            The reference from the allow-list.

        Raises
        ------
        EngineError
            If the pull did not bring it.
        """
        # Slashes, the tag's colon and the digest's @ stay as they are, as the Docker CLI sends
        # them.
        path = f"/images/{quote(image, safe='/:@')}/json"
        status, _ = self.engine.request("GET", path)
        if status == 200:
            return
        self.engine.request(
            "POST", "/images/create", query={"fromImage": image}, timeout=_PULL_SECONDS
        )
        status, _ = self.engine.request("GET", path)
        if status != 200:
            raise EngineError(
                f"Docker could not pull the task image {image}; check that the host reaches its "
                "registry, or pull it once on the host"
            )

    def _remove_labelled(self, wanted: dict[str, str], *, spare_live: bool) -> int:
        """Remove containers and networks with these labels.

        Parameters
        ----------
        wanted
            Label to value.
        spare_live
            Leave alone what a live connection supervises.

        Returns
        -------
        int
            How many containers were removed.
        """
        filters = json.dumps({"label": [f"{k}={v}" for k, v in sorted(wanted.items())]})
        _, containers = self.engine.request(
            "GET", "/containers/json", query={"all": "1", "filters": filters}
        )
        _, networks = self.engine.request("GET", "/networks", query={"filters": filters})
        # The live runs are read after the listing: a run registers before it creates anything,
        # so everything listed that belongs to a live run is found among them.
        with self._lock:
            live = set(self._live) if spare_live else set()
        removed = 0
        for container in containers if isinstance(containers, list) else []:
            names = {str(n).lstrip("/") for n in container.get("Names", [])}
            if names & live:
                continue
            self._remove_container(str(container["Id"]))
            removed += 1
        for network in networks if isinstance(networks, list) else []:
            if network.get("Name") not in live:
                self._close_network(str(network["Name"]))
        return removed

    # --- networks -----------------------------------------------------------------------

    def _open_network(
        self, kind: str, ident: str, owner: str, service: tuple[str, str] | None
    ) -> str:
        """Create a task's or build's network and attach a service to it.

        Parameters
        ----------
        kind
            ``task`` or ``build``.
        ident
            Its id.
        owner
            The worker.
        service
            Compose service and alias to attach, or `None`.

        Returns
        -------
        str
            The network's name.

        Raises
        ------
        EngineError
            If Docker refuses it or the service is not running; nothing is left behind.
        """
        name = container_name(self.settings, kind, ident)
        status, body = self.engine.request(
            "POST", "/networks/create", network_spec(self.settings, kind, ident, owner)
        )
        if status != 201:
            raise EngineError(f"Docker refused the {kind} network: {_message(body)}")
        if service is not None:
            try:
                cid = self._service_container(service[0])
                status, body = self.engine.request(
                    "POST",
                    f"/networks/{quote(name, safe='')}/connect",
                    {"Container": cid, "EndpointConfig": {"Aliases": [service[1]]}},
                )
                if status != 200:
                    raise EngineError(f"Docker could not attach {service[0]}: {_message(body)}")
            except EngineError:
                self._close_network(name)
                raise
        return name

    def _service_container(self, service: str) -> str:
        """Find the running container of a Compose service of this stack.

        Parameters
        ----------
        service
            The service name.

        Returns
        -------
        str
            Its id.

        Raises
        ------
        EngineError
            If it is not running.
        """
        filters = json.dumps(
            {
                "label": [
                    f"com.docker.compose.project={self.settings.stack}",
                    f"com.docker.compose.service={service}",
                ],
                "status": ["running"],
            }
        )
        status, body = self.engine.request("GET", "/containers/json", query={"filters": filters})
        if status != 200 or not isinstance(body, list) or not body:
            raise EngineError(
                f"the {service} service of this stack is not running; start it "
                f"(docker compose up -d {service})"
            )
        return str(body[0]["Id"])

    def _close_network(self, name: str) -> None:
        """Detach everything from a network and remove it.

        Parameters
        ----------
        name
            The network.
        """
        path = f"/networks/{quote(name, safe='')}"
        try:
            status, body = self.engine.request("GET", path)
            if status == 200 and isinstance(body, dict):
                for cid in body.get("Containers") or {}:
                    self.engine.request(
                        "POST", f"{path}/disconnect", {"Container": cid, "Force": True}
                    )
            self.engine.request("DELETE", path)
        except EngineError as exc:
            logger.error("Could not remove network %s: %s", name, exc)

    # --- environments -------------------------------------------------------------------

    def _marker(self, volume: str) -> Path:
        """Locate an environment's completion marker in the launcher's own volume.

        Parameters
        ----------
        volume
            The environment volume.

        Returns
        -------
        Path
            The marker; its mtime is the last use.
        """
        return self.settings.state_dir / "environments" / f"{volume}.complete"

    def _built(self, volume: str) -> bool:
        """Say whether an environment is built: its marker and its volume both exist.

        Parameters
        ----------
        volume
            The volume.

        Returns
        -------
        bool
            Whether a build ended well and its volume is still there. A build container cannot
            write the marker: it lives outside every volume a build mounts. A marker without a
            volume (a ``docker volume prune``) is a build that has to run again.
        """
        if not self._marker(volume).is_file():
            return False
        try:
            status, _ = self.engine.request("GET", f"/volumes/{quote(volume, safe='')}")
        except EngineError:
            return False
        return status == 200

    def _mark_complete(self, volume: str) -> None:
        """Write the marker of a finished build.

        Parameters
        ----------
        volume
            The volume.
        """
        marker = self._marker(volume)
        temporary = marker.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(volume + "\n", encoding="utf-8")
        os.replace(temporary, marker)

    def _touch(self, volume: str) -> None:
        """Record a use of a built environment.

        Parameters
        ----------
        volume
            The volume.
        """
        with contextlib.suppress(FileNotFoundError):
            os.utime(self._marker(volume))

    def _remove_volume(self, volume: str) -> bool:
        """Remove an environment volume unless a container uses it.

        Parameters
        ----------
        volume
            The volume.

        Returns
        -------
        bool
            Whether it is gone.
        """
        try:
            status, _ = self.engine.request("DELETE", f"/volumes/{quote(volume, safe='')}")
        except EngineError as exc:
            logger.error("Could not remove volume %s: %s", volume, exc)
            return False
        return status in (204, 404)

    def _evict(self, *, keep: str) -> None:
        """Remove the least recently used environments beyond `keep_environments`.

        Parameters
        ----------
        keep
            The volume just built, never removed here.
        """
        markers = sorted(
            (self.settings.state_dir / "environments").glob("*.complete"),
            key=_mtime,
            reverse=True,
        )
        for marker in markers[self.settings.keep_environments :]:
            volume = marker.name.removesuffix(".complete")
            if volume != keep and self._remove_volume(volume):
                marker.unlink(missing_ok=True)
