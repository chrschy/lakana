"""A Docker Engine stand-in for the launcher's unit tests, and a settings factory."""
# ruff: file-ignore[any-type]  (Docker's JSON bodies and the tests' hooks are untyped by nature)

from __future__ import annotations

import json
import socket
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from lakana_worker.launcher.settings import LauncherSettings


def settings(state: Path, **changes: Any) -> LauncherSettings:
    """Build launcher settings for a test, with its state under `state`.

    Parameters
    ----------
    state
        The test's own directory.
    **changes
        Fields to replace.

    Returns
    -------
    LauncherSettings
        Settings with one allowed image.
    """
    values: dict[str, Any] = {
        "stack": "lakana",
        "images": {"3.13": "ghcr.io/chrschy/lakana-task:0.14.0-py3.13"},
        "work_volume": "lakana-task-work",
        "socket": Path("l.sock"),
        "state_dir": state,
    }
    values.update(changes)
    return LauncherSettings(**values)


def _labels_match(labels: dict[str, str], query: dict[str, Any] | None) -> bool:
    """Say whether labels satisfy a ``filters`` query's ``label`` list.

    Parameters
    ----------
    labels
        An object's labels.
    query
        The query parameters.

    Returns
    -------
    bool
        Whether every ``key=value`` of the filter is among the labels.
    """
    wanted = json.loads((query or {}).get("filters", "{}")).get("label", [])
    return all(labels.get(item.split("=", 1)[0]) == item.split("=", 1)[1] for item in wanted)


@dataclass
class FakeEngine:
    """Answers the launcher's Engine API calls from its own small state, recording each one.

    Parameters
    ----------
    version
        What ``GET /version`` reports.
    output
        What every container writes to stdout.
    exit_code
        Every container's exit status, unless it is stopped (137).
    oom
        Whether ``GET /containers/{id}/json`` reports an out-of-memory kill.
    no_new_privileges_works
        Whether a container with that option starts.
    hold
        Whether a container's output waits until it is stopped, killed or removed.
    services
        Compose service name to the id of its running container.
    pull_works
        Whether pulling an image brings it.
    isolated
        ``works``, ``refused`` (Docker refuses the isolated gateway mode) or ``ignored`` (Docker
        accepts the network but drops the option, as an engine without the mode does).
    probe_exits
        Ingestion-root probe: root name to the exit status of its probe container (0 when absent).
    hooks
        Callables run at a named moment: ``pull``, ``created`` (a task container), ``attach``,
        ``start`` (a task container), each a window in which a caller can leave.
    volumes
        Volumes that exist.
    calls
        Every call: method, path, body or query.
    containers
        Container id to its name, labels, spec and status.
    networks
        Network name to its labels and attached containers.
    images
        Images present.
    stdin
        What each container received on stdin.
    """

    version: str = "29.8.0"
    output: list[bytes] = field(default_factory=lambda: [b"hello\n"])
    exit_code: int = 0
    oom: bool = False
    no_new_privileges_works: bool = True
    hold: bool = False
    services: dict[str, str] = field(
        default_factory=lambda: {"server": "c-server", "egress": "c-egress"}
    )
    pull_works: bool = True
    isolated: str = "works"
    probe_exits: dict[str, int] = field(default_factory=dict)
    hooks: dict[str, Callable[[], None]] = field(default_factory=dict)
    volumes: set[str] = field(default_factory=set)
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    containers: dict[str, dict[str, Any]] = field(default_factory=dict)
    networks: dict[str, dict[str, Any]] = field(default_factory=dict)
    images: set[str] = field(default_factory=lambda: {"ghcr.io/chrschy/lakana-task:0.14.0-py3.13"})
    stdin: list[bytes] = field(default_factory=list)
    _released: dict[str, threading.Event] = field(default_factory=dict)
    _count: int = 0

    def created(self, kind: str) -> list[dict[str, Any]]:
        """List the specs of every container of one kind created so far.

        Parameters
        ----------
        kind
            ``task``, ``build`` or ``probe``.

        Returns
        -------
        list of dict
            The specs, in order.
        """
        return [
            body
            for method, path, body in self.calls
            if path == "/containers/create" and body["Labels"]["lakana.launcher.kind"] == kind
        ]

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: dict[str, Any] | None = None,
        timeout: float | None = 60.0,
    ) -> tuple[int, Any]:
        """Answer one call.

        Parameters
        ----------
        method
            The HTTP method.
        path
            The path after the API version.
        body
            The JSON body.
        query
            The query parameters.
        timeout
            Ignored.

        Returns
        -------
        tuple
            Status and decoded body.
        """
        self.calls.append((method, path, body if body is not None else query))
        query = query if query is not None else {}
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if parts == ["version"]:
            return 200, {"Version": self.version}
        if parts[0] == "images":
            if method == "POST":
                self._hook("pull")
                if self.pull_works:
                    self.images.add(query["fromImage"])
                return 200, b"{}"
            image = "/".join(parts[1:-1])  # the reference keeps its slashes in the path
            return (200, {}) if image in self.images else (404, {"message": "No such image"})
        if parts == ["containers", "create"]:
            self._count += 1
            cid = f"c{self._count}"
            self.containers[cid] = {
                "Name": query["name"],
                "Labels": body["Labels"],
                "spec": body,
                "exit": None,
            }
            self._released[cid] = threading.Event()
            self.volumes.update(
                m["Source"] for m in body["HostConfig"]["Mounts"] if m["Type"] == "volume"
            )
            if body["Labels"]["lakana.launcher.kind"] == "task":
                self._hook("created")
            return 201, {"Id": cid}
        if parts == ["containers", "json"]:
            wanted = json.loads(query.get("filters", "{}")).get("label", [])
            service = next(
                (w.split("=", 1)[1] for w in wanted if w.startswith("com.docker.compose.service=")),
                None,
            )
            if service is not None:
                return 200, [{"Id": self.services[service]}] if service in self.services else []
            return 200, [
                {"Id": cid, "Names": ["/" + c["Name"]], "Labels": c["Labels"]}
                for cid, c in self.containers.items()
                if _labels_match(c["Labels"], query)
            ]
        if parts[0] == "containers":
            return self._container(method, parts[1], parts[2] if len(parts) > 2 else None)
        if parts == ["networks", "create"]:
            if self.isolated == "refused":
                return 400, {"message": "invalid option gateway_mode_ipv4"}
            self.networks[body["Name"]] = {"Labels": body["Labels"], "Containers": {}, "spec": body}
            return 201, {"Id": body["Name"]}
        if parts == ["networks"]:
            return 200, [
                {"Name": name}
                for name, n in self.networks.items()
                if _labels_match(n["Labels"], query)
            ]
        if parts[0] == "networks":
            return self._network(method, parts[1], parts[2] if len(parts) > 2 else None, body)
        if parts[0] == "volumes" and method == "GET":
            return (
                (200, {"Name": parts[1]}) if parts[1] in self.volumes else (404, {"message": "no"})
            )
        if parts[0] == "volumes" and method == "DELETE":
            mounted = any(
                m["Source"] == parts[1]
                for c in self.containers.values()
                for m in c["spec"]["HostConfig"]["Mounts"]
            )
            if mounted:
                return 409, {"message": "volume is in use"}
            self.volumes.discard(parts[1])
            return 204, b""
        return 500, {"message": f"the fake does not know {method} {path}"}

    def _hook(self, moment: str) -> None:
        """Run the callable registered for a moment, if any.

        Parameters
        ----------
        moment
            The moment's name.
        """
        hook = self.hooks.get(moment)
        if hook is not None:
            hook()

    def _container(self, method: str, cid: str, action: str | None) -> tuple[int, Any]:
        """Answer a call on one container.

        Parameters
        ----------
        method
            The HTTP method.
        cid
            The container id.
        action
            ``start``, ``wait``, ``json``, ``kill``, ``stop`` or `None` (delete).

        Returns
        -------
        tuple
            Status and body.
        """
        container = self.containers.get(cid)
        if container is None:
            return 404, {"message": "No such container"}
        if action == "start":
            options = container["spec"]["HostConfig"]["SecurityOpt"]
            if "no-new-privileges" in options and not self.no_new_privileges_works:
                return 400, {"message": "exec /usr/local/bin/python: operation not permitted"}
            if container["Labels"]["lakana.launcher.kind"] == "task":
                self._hook("start")
            return 204, b""
        if action == "wait":
            labels = container["Labels"]
            if labels["lakana.launcher.kind"] == "probe":
                root = labels["lakana.launcher.id"].removeprefix("ingest-")
                return 200, {"StatusCode": self.probe_exits.get(root, 0)}
            if self.hold and container["exit"] is None:
                self._released[cid].wait(10)
            return 200, {
                "StatusCode": container["exit"] if container["exit"] is not None else self.exit_code
            }
        if action == "json":
            return 200, {"State": {"OOMKilled": self.oom}}
        if action in ("kill", "stop"):
            container["exit"] = 137
            self._released[cid].set()
            return 204, b""
        if method == "DELETE":
            self._released[cid].set()
            del self.containers[cid]
            for network in self.networks.values():
                network["Containers"].pop(cid, None)
            return 204, b""
        return 500, {"message": f"the fake does not know {action}"}

    def _network(self, method: str, name: str, action: str | None, body: Any) -> tuple[int, Any]:
        """Answer a call on one network.

        Parameters
        ----------
        method
            The HTTP method.
        name
            The network.
        action
            ``connect``, ``disconnect`` or `None`.
        body
            The request body.

        Returns
        -------
        tuple
            Status and body.
        """
        network = self.networks.get(name)
        if network is None:
            return 404, {"message": "No such network"}
        if action == "connect":
            network["Containers"][body["Container"]] = body["EndpointConfig"]["Aliases"]
            return 200, b""
        if action == "disconnect":
            network["Containers"].pop(body["Container"], None)
            return 200, b""
        if method == "GET":
            options = (
                {} if self.isolated == "ignored" else network.get("spec", {}).get("Options", {})
            )
            return 200, {
                "Name": name,
                "Containers": dict(network["Containers"]),
                "Options": options,
            }
        del self.networks[name]
        return 204, b""

    def attach(self, container_id: str, *, stdin: bool) -> socket.socket:
        """Hand out a stream that carries the container's output as Docker frames.

        Parameters
        ----------
        container_id
            The container.
        stdin
            Whether the launcher will write to it.

        Returns
        -------
        socket.socket
            The launcher's end.
        """
        self._hook("attach")
        ours, theirs = socket.socketpair()

        def feed() -> None:
            """Read stdin, wait if holding, write the frames, close."""
            if stdin:
                received = b""
                while chunk := theirs.recv(65536):
                    received += chunk
                self.stdin.append(received)
            if self.hold:
                self._released[container_id].wait(10)
            try:
                if not self.hold or self.containers.get(container_id, {}).get("exit") is None:
                    for chunk in self.output:
                        theirs.sendall(struct.pack(">BxxxI", 1, len(chunk)) + chunk)
            except OSError:
                pass  # the launcher closed its end: the caller left
            finally:
                theirs.close()

        threading.Thread(target=feed, daemon=True).start()
        return ours
