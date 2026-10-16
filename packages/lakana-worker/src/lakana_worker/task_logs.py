"""A task's free-text log in object storage: best-effort appends.

Split out of `lakana_worker.tasks`, which re-exports `write_task_log`.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from lakana_core.identifiers import shown
from lakana_core.runs.service import task_log_path
from lakana_core.secrets.keys import KeyProvider
from lakana_core.storage.resolver import StorageResolver

logger = logging.getLogger("lakana_worker.tasks")
"""The name `lakana_worker.tasks` logged under before the split, kept for log filters."""


def _append_log(
    session: Session,
    key_provider: KeyProvider | None,
    environment: str,
    run_id: uuid.UUID,
    asset: str,
    block: str,
) -> None:
    """Append `block` to a task's free-text log (read, concatenate, write).

    A task's attempts never overlap, so there is no concurrent writer.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Decrypts secret-backed storage.
    environment
        Environment name.
    run_id
        The run.
    asset
        The task.
    block
        Text to append.
    """
    storage = StorageResolver(session, key_provider).for_environment(environment).storage
    path = task_log_path(run_id, asset)
    existing = storage.read(path) if storage.exists(path) else b""
    storage.write(path, existing + block.encode("utf-8"))


def write_task_log(
    session: Session,
    key_provider: KeyProvider | None,
    environment: str,
    run_id: uuid.UUID,
    asset: str,
    block: str,
) -> None:
    """Append to a task's log, logging (never raising) a failure.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Decrypts secret-backed storage.
    environment
        Environment name.
    run_id
        The run.
    asset
        The task.
    block
        Text to append.
    """
    try:
        _append_log(session, key_provider, environment, run_id, asset, block)
    except Exception:
        logger.warning(
            "Could not write the log of %s in run %s.", shown(asset), run_id, exc_info=True
        )
