"""Verify what a task is about to run: its project version's code tarball and manifest version.

`verify_code` checks the tarball before a run's first task;
`check_manifest_version` refuses a manifest this worker cannot read. Split out of
`lakana_worker.tasks`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from sqlalchemy.orm import Session

from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.models import Environment, JobVersion
from lakana_core.projects.manifest import MANIFEST_VERSION
from lakana_core.runs.tasks import FailureKind
from lakana_core.secrets.keys import KeyProvider
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.task_outcome import TaskFailure


def verify_code(
    session: Session,
    key_provider: KeyProvider | None,
    environment: Environment,
    version: JobVersion,
) -> str | None:
    """Check a project version's code tarball against its `code_sha256`.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Decrypts secret-backed storage.
    environment
        The run's environment.
    version
        The project version.

    Returns
    -------
    str or None
        Why the code cannot be trusted, or `None` when it matches.

    Raises
    ------
    OSError
        For a storage failure other than a missing object (retryable).
    """
    root = environment.storage_root.rstrip("/") + "/"
    if not version.code_uri.startswith(root):
        return "the code tarball is not under the environment's storage root"
    storage = StorageResolver(session, key_provider).for_environment(environment.name).storage
    try:
        data = storage.read(version.code_uri[len(root) :])
    except NotFoundError:
        return "the code tarball is missing"
    except ValidationError:
        return "the code tarball's location is not a valid storage path"
    if hashlib.sha256(data).hexdigest() != version.code_sha256:
        return "the code tarball does not match its recorded code_sha256"
    return None


def check_manifest_version(manifest: Mapping[str, object]) -> None:
    """Refuse a project version whose manifest this worker cannot read.

    Parameters
    ----------
    manifest
        The project version's manifest; untrusted.

    Raises
    ------
    TaskFailure
        Permanent when `version` is present and is not an integer from 1 to
        `MANIFEST_VERSION`: a higher one could mean something this worker
        would run wrongly (an output it does not know as a table write), and a
        zero, a negative number or a non-integer was never written by a deploy.
    """
    declared = manifest.get("version")
    if declared is None:
        return
    if (
        isinstance(declared, int)
        and not isinstance(declared, bool)
        and 1 <= declared <= MANIFEST_VERSION
    ):
        return
    raise TaskFailure(
        FailureKind.PERMANENT,
        f"the project version's manifest has version {ascii(declared)[:30]}, but this worker "
        f"understands versions 1 to {MANIFEST_VERSION}; update the worker to the platform "
        "version that deployed it, or redeploy with this worker's version",
    )
