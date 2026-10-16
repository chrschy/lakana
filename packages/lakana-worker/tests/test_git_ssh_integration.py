"""SSH fetch against a real OpenSSH server in a container."""

from __future__ import annotations

import io
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from testcontainers.core.container import DockerContainer
from testcontainers.core.image import DockerImage
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from worker_git_helpers import git, make_repo

from lakana_core.models import RepositoryKind
from lakana_core.projects.limits import DeployLimits
from lakana_worker.git import GitCredential, GitError, fetch_commit

pytestmark = pytest.mark.integration


def _tar_of(directory: Path, arcname: str) -> bytes:
    """Tar a directory into memory.

    Parameters
    ----------
    directory
        The directory.
    arcname
        Its name inside the archive.

    Returns
    -------
    bytes
        The tar file.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as handle:
        handle.add(directory, arcname=arcname)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def sshd(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    """Start the SSH git server with one repository and one deploy key.

    Parameters
    ----------
    tmp_path_factory
        Pytest's temp directory factory.

    Yields
    ------
    dict of str to str
        ``url``, ``sha``, ``private_key``, ``known_hosts``.
    """
    work = tmp_path_factory.mktemp("sshd")
    sha = make_repo(work / "src", {"assets/bronze/events.sql": "SELECT 1 AS id\n"})
    git(work, "clone", "--quiet", "--bare", str(work / "src"), str(work / "analytics.git"))
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    with DockerImage(path=Path(__file__).parent / "sshd", tag="lakana-test-sshd:latest") as image:
        container = (
            DockerContainer(str(image), _wait_strategy=LogMessageWaitStrategy("Server listening"))
            .with_exposed_ports(22)
            .with_copy_into_container(f"{public}\n".encode(), "/home/git/.ssh/authorized_keys")
            .with_copy_into_container(
                _tar_of(work / "analytics.git", "analytics.git"), "/tmp/repo.tar"
            )
        )
        with container:
            container.exec(
                [
                    "sh",
                    "-c",
                    "tar -xf /tmp/repo.tar -C /home/git && "
                    "chown -R git:git /home/git && chmod 700 /home/git/.ssh && "
                    "chmod 600 /home/git/.ssh/authorized_keys",
                ]
            )
            host_key = container.exec(["cat", "/etc/ssh/ssh_host_ed25519_key.pub"]).output.decode()
            host, port = container.get_container_host_ip(), container.get_exposed_port(22)
            key_type, key_body = host_key.split()[:2]
            yield {
                "url": f"ssh://git@{host}:{port}/home/git/analytics.git",
                "sha": sha,
                "private_key": private,
                "known_hosts": f"[{host}]:{port} {key_type} {key_body}\n",
            }


def _fetch(tmp_path: Path, sshd: dict[str, str], known_hosts: str) -> str:
    """Fetch the served repository over SSH.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    sshd
        The fixture's values.
    known_hosts
        The known_hosts text to trust.

    Returns
    -------
    str
        The fetched SHA.
    """
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    return fetch_commit(
        kind=RepositoryKind.SSH,
        url=sshd["url"],
        ref=None,
        credential=GitCredential(private_key=sshd["private_key"]),
        known_hosts=known_hosts,
        workdir=work,
        limits=DeployLimits(),
        repo_roots=[],
        deadline=time.monotonic() + 120,
    ).sha


def test_deploy_key_fetches_the_commit(tmp_path: Path, sshd: dict[str, str]) -> None:
    """A deploy key with the right known_hosts entry fetches HEAD."""
    assert _fetch(tmp_path, sshd, sshd["known_hosts"]) == sshd["sha"]


def test_unknown_host_key_is_refused(tmp_path: Path, sshd: dict[str, str]) -> None:
    """StrictHostKeyChecking=yes: a different trusted key refuses the connection."""
    stranger = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    host_part = sshd["known_hosts"].split()[0]
    with pytest.raises(GitError, match="host key verification failed"):
        _fetch(tmp_path, sshd, f"{host_part} {stranger}\n")
