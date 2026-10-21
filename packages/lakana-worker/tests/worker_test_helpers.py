"""Shared helpers for the lakana-worker test suite.

The module name is deliberately repo-unique, matching
`lakana-core/tests/core_test_helpers.py`'s own naming for the same reason: this
package's `tests/` directory has no `__init__.py`, so pytest's default
"prepend" import mode resolves a bare top-level module name first-wins
across the whole workspace's `sys.path` — a second `helpers.py` (or a second
`core_test_helpers.py`) here would collide with lakana-core's.
"""

import json
import os
import uuid
from collections.abc import Mapping

from minio import Minio
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable
from lakana_core.errors import PermissionDenied


class ActionAllowList:
    """Permits exactly the listed actions, on any securable, and records every check.

    Parameters
    ----------
    *allowed
        The actions to permit.
    """

    def __init__(self, *allowed: Action) -> None:
        self.allowed = frozenset(allowed)
        self.seen: list[tuple[Action, Securable]] = []

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Record the check and refuse any action not in the allow list.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            If `action` is not allowed.
        """
        self.seen.append((action, securable))
        if action not in self.allowed:
            raise PermissionDenied(f"may not {action.value}")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, without recording.

        Parameters
        ----------
        principal
            The actor the question is about.
        action
            The action in question.
        securable
            The object in question.

        Returns
        -------
        bool
            Whether `action` is allowed.
        """
        return action in self.allowed


class SeaweedFsContainer(DockerContainer):
    """A SeaweedFS container exposing an S3-compatible gateway.

    Mirrors the interface of testcontainers' S3 container class
    (`get_client`, `get_config`, `.access_key`/`.secret_key`) so integration
    tests can swap one for the other without touching call sites. Unlike
    some other S3 servers, SeaweedFS's S3 gateway refuses anonymous requests
    outright once an identity config is supplied, so a single-identity config file is
    generated per instance and copied into the container via the Docker API
    (`with_copy_into_container`, applied after the container is created but
    before its main process starts) rather than a host bind mount — the
    config never touches the host filesystem as a named path at all, which
    also sidesteps host/daemon path-translation issues a bind mount can hit
    in sandboxed or remote-daemon Docker setups.
    """

    def __init__(
        self,
        image: str = "chrislusf/seaweedfs:4.47",
        port: int = 8333,
        access_key: str = "lakanatest",
        secret_key: str = "lakanatestsecret12345",
    ) -> None:
        """Configure a SeaweedFS container; does not start it.

        Parameters
        ----------
        image
            Docker image to use.
        port
            Port the S3 gateway listens on inside the container.
        access_key
            Access key for the one identity this container is configured
            with.
        secret_key
            Secret key for that identity.
        """
        super().__init__(image, _wait_strategy=LogMessageWaitStrategy("Starting S3 API Server"))
        self.port = port
        self.access_key = access_key
        self.secret_key = secret_key

        identity_config = json.dumps(
            {
                "identities": [
                    {
                        "name": "lakanatest",
                        "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
                        "actions": ["Admin", "Read", "Write"],
                    }
                ]
            }
        )
        self.with_exposed_ports(self.port)
        self.with_copy_into_container(identity_config.encode(), "/etc/seaweedfs/s3.json")
        self.with_command(
            f"server -s3 -s3.port={self.port} -s3.config=/etc/seaweedfs/s3.json "
            "-dir=/data -ip.bind=0.0.0.0"
        )

    def get_client(self) -> Minio:
        """Return a `minio`-SDK client for the running container.

        Returns
        -------
        Minio
            A client for this container's one configured identity. The
            `minio` SDK speaks plain S3, so it works against SeaweedFS's
            gateway the same way it works against any S3 server.
        """
        config = self.get_config()
        return Minio(
            endpoint=config["endpoint"],
            access_key=config["access_key"],
            secret_key=config["secret_key"],
            secure=False,
        )

    def get_config(self) -> dict[str, str]:
        """Return this container's endpoint and credentials.

        Returns
        -------
        dict[str, str]
            `endpoint`, `access_key`, `secret_key`.
        """
        host_ip = self.get_container_host_ip()
        exposed_port = self.get_exposed_port(self.port)
        return {
            "endpoint": f"{host_ip}:{exposed_port}",
            "access_key": self.access_key,
            "secret_key": self.secret_key,
        }


class ReadsById(AllowAllAuthorization):
    """Allow-all, except that `READ_TABLE` is decided by the principal's id.

    Parameters
    ----------
    readers
        The ids of the principals that may read a table.
    """

    def __init__(self, readers: set[uuid.UUID]) -> None:
        self.readers = readers

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Refuse `READ_TABLE` to anyone not in `readers`.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            Whether the actor may do it.
        """
        return action is not Action.READ_TABLE or principal.id in self.readers

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Raise as `permits` answers, so that a query's own read check agrees with it.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Raises
        ------
        PermissionDenied
            When `permits` is false.
        """
        if not self.permits(principal, action, securable):
            raise PermissionDenied("denied")


def local_prefixes(parameters: Mapping[str, object]) -> tuple[str, ...]:
    """Name the directory of every local file a rewritten query reads, as read prefixes.

    The query process opens only the locations it is given (allow-list mode). A test that hands
    `execute_query` local files of its own gives it their directories, as the pipeline gives it
    the locations of the tables a query reads.

    Parameters
    ----------
    parameters
        The rewritten query's variables; every list among them is a list of files.

    Returns
    -------
    tuple of str
        Each file's directory followed by ``/``, sorted, without duplicates.
    """
    return tuple(
        sorted(
            {
                os.path.dirname(url) + "/"
                for value in parameters.values()
                if isinstance(value, list)
                for url in value
            }
        )
    )
