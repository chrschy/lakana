"""What the server's readiness route checks: the database, the storage and a worker.

The route itself (`lakana_server.readiness`) runs these with a time limit each and caches the
answer; this module only reads and probes. One call of `read_database` is one connection and at
most `2 + MAX_PROBED_ENVIRONMENTS` short statements (the time, one alive worker, the
environments and the secret of each that needs one); one call of `probe_storage` is one request
to a storage, which reads no object.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.models import Environment
from lakana_core.models.environment import LIVE_ENVIRONMENT
from lakana_core.runs.workers import any_alive
from lakana_core.secrets.keys import KeyProvider
from lakana_core.storage.resolver import ResolvedStorage, StorageResolver

logger = logging.getLogger(__name__)

READINESS_CHECKS = ("database", "storage", "worker")
"""The names the readiness route can list as failed, in the order it lists them."""

MAX_PROBED_ENVIRONMENTS = 5
"""Most environments whose storage one readiness check probes (the first by name)."""


@dataclass(frozen=True)
class StorageTarget:
    """One environment's storage, as the readiness check probes it.

    Parameters
    ----------
    environment
        The environment's name.
    storage
        Its storage, or `None` when it could not be set up (the environment does not exist, or
        its storage secret is missing or cannot be decrypted): the check fails for it.
    """

    environment: str
    storage: ResolvedStorage | None


@dataclass(frozen=True)
class DatabaseFindings:
    """What one readiness check read from the database.

    Parameters
    ----------
    worker_alive
        Whether at least one worker's heartbeat is younger than the lease.
    storages
        The storages to probe; empty when there is no environment yet.
    """

    worker_alive: bool
    storages: tuple[StorageTarget, ...]


def read_database(
    engine: Engine, *, lease_seconds: int, environments: Sequence[str], key_provider: KeyProvider
) -> DatabaseFindings:
    """Prove that the database answers, and read whether a worker is alive and what to probe.

    Parameters
    ----------
    engine
        The metadata database.
    lease_seconds
        `LAKANA_RUN__LEASE_SECONDS`: a worker is alive while its heartbeat is younger.
    environments
        The environments whose storage counts (`LAKANA_READINESS__ENVIRONMENTS`); empty means
        every live one. Either way at most `MAX_PROBED_ENVIRONMENTS`, the first by name. A
        name given here that no live environment has is a target without a storage, so the
        check fails for it.
    key_provider
        Decrypts the storage secret of an environment that has one.

    Returns
    -------
    DatabaseFindings
        Whether a worker is alive, and the storages to probe.
    """
    with Session(engine) as session:
        now = db_now(session)
        worker_alive = any_alive(session, now, lease_seconds)
        query = select(Environment.name).where(LIVE_ENVIRONMENT)
        if environments:
            query = query.where(Environment.name.in_(list(environments)))
        live = list(session.scalars(query.order_by(Environment.name)))
        names = sorted({*live, *environments})[:MAX_PROBED_ENVIRONMENTS]
        resolver = StorageResolver(session, key_provider)
        targets = []
        for name in names:
            storage = None
            if name in live:
                try:
                    storage = resolver.for_environment(name)
                except Exception as exc:  # a missing secret or a wrong key fails the check
                    logger.debug(
                        "The storage of environment %r could not be set up for the readiness "
                        "check (%s).",
                        name,
                        type(exc).__name__,
                    )
            targets.append(StorageTarget(name, storage))
    return DatabaseFindings(worker_alive=worker_alive, storages=tuple(targets))


def probe_storage(storage: ResolvedStorage) -> None:
    """Ask a storage about its root, which proves that it answers and accepts the credentials.

    One metadata request (on S3 a `HEAD` and at most a one-key listing), through the same
    client a table read uses; no object is read or written. A root that does not exist yet (an
    environment with no table) is an answer too. On a storage on the server's own disk this
    proves little: the path is only looked up.

    Parameters
    ----------
    storage
        The environment's storage.
    """
    storage.file_io.new_input(storage.config.root).exists()
