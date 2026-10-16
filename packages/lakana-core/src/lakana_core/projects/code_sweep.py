"""The code archive sweep: remove what a failed deploy left under ``_artifacts/code/``.

A deploy uploads its code archive to ``_artifacts/code/<repository>/<sha>-<digest>.tar.gz``
before the database records the project version that names it. A deploy that fails afterwards, or a
worker that is killed, leaves the archive behind; nothing else removes it. Run by the worker
that holds the scheduler role, next to `lakana_core.artifacts.sweep.sweep_artifacts`, with the
same clock (the database's), the same grace period and the same rotation of environments.

It deletes bytes unattended, so the rule is: **an archive a project version names is never deleted,
and neither is anything a deploy that is running could be about to name.** Project versions are
never deleted, so an archive one names is never garbage. An object is deleted only if all of
these hold:

1. its path is exactly ``_artifacts/code/<repository>/<40 hex>-<64 hex>.tar.gz``; anything else
   under the prefix is left alone and counted;
2. its modification time is older than `ORPHAN_BLOB_GRACE` (seven days) by the sweep's ``now``;
   an object without a modification time is left alone;
3. no project version in any environment names it: neither by ``code_uri`` equal to the object's URI
   (built the way a deploy builds it: the environment's storage root without a trailing ``/``,
   then the path) nor by ``commit_sha`` and ``code_sha256`` equal to the two hex parts of the
   object's name. The second rule makes the decision independent of how a root is spelled (two
   environments on one location, a symlink): the digest makes the name unique by content, and
   the price is at most one orphan kept. Environments that share a storage root share the
   objects;
4. no deploy run (not a dry run, not a rollback: they upload nothing) is ``queued`` or
   ``running`` in any environment that uses this storage root; such a root is skipped and
   counted as busy.

Conditions 4 and 3 are read again, in that order, in one transaction immediately before each
delete: a deploy records its project version and ends its run in one commit, so a deploy that
finished between the two reads shows up in the second. A deploy that rewrites an old object
(`Storage.write` replaces it and refreshes its age) is covered by its run row.

One window remains: a delete that is still in flight (a slow object store retrying) when a deploy
of the same code is submitted, uploads and records. The deploy checks that its archive exists
after it recorded the project version and writes it again if not, and a redeploy of the same commit
does the same check, so a delete that lands later than the deploy's check is repaired by the
next deploy of that commit (`lakana_worker.deploy`).

Failures never stop the run: a listing or delete that fails is logged and counted, the other
objects and storage roots go on, and `MAX_CONSECUTIVE_FAILURES` in a row give a root up for the
run. The run has a deadline of its own, checked before every decision.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from lakana_core.artifacts.sweep import MAX_CONSECUTIVE_FAILURES, ORPHAN_BLOB_GRACE
from lakana_core.identifiers import shown
from lakana_core.models import DeployKind, DeployRun, DeployStatus, Environment, JobVersion
from lakana_core.storage.base import Storage
from lakana_core.storage.resolver import StorageResolver

logger = logging.getLogger(__name__)

CODE_PREFIX = "_artifacts/code"
"""Where a deploy puts code archives, relative to an environment's storage root."""
CODE_SWEEP_MAX_SECONDS = 60.0
"""Default length of one run; shutdown ends it earlier."""

_ARCHIVE = re.compile(
    re.escape(CODE_PREFIX) + r"/[^/\x00-\x1f\x7f]+/[0-9a-f]{40}-[0-9a-f]{64}\.tar\.gz"
)
_BATCH = 500
"""Archives whose names are looked up in the database in one query."""


class _Spent(Exception):
    """The run's deadline has passed, or shutdown was asked for."""


class _Busy(Exception):
    """A deploy is queued or running in an environment that uses the storage root."""


@dataclass(frozen=True)
class CodeSweepReport:
    """What one run did.

    Parameters
    ----------
    deleted
        Archives removed.
    freed_bytes
        Their total size.
    left_alone
        Objects under the prefix that are not archives by name; never touched.
    failed_roots
        Storage roots skipped because the listing, the database or too many deletes failed.
    busy_roots
        Storage roots skipped because a deploy was queued or running.
    stopped_early
        Whether the deadline or a shutdown ended the run before it was done.
    """

    deleted: int = 0
    freed_bytes: int = 0
    left_alone: int = 0
    failed_roots: int = 0
    busy_roots: int = 0
    stopped_early: bool = False


def _active_deploy(session: Session, environment_ids: list[uuid.UUID]) -> bool:
    """Say whether a deploy that uploads code is queued or running in any of the environments.

    Parameters
    ----------
    session
        Database session.
    environment_ids
        The environments that share one storage root.

    Returns
    -------
    bool
        Whether such a run exists.
    """
    return (
        session.scalar(
            select(DeployRun.id)
            .where(
                DeployRun.environment_id.in_(environment_ids),
                DeployRun.kind == DeployKind.DEPLOY,
                DeployRun.status.in_((DeployStatus.QUEUED, DeployStatus.RUNNING)),
            )
            .limit(1)
        )
        is not None
    )


def _code_uri(storage_root: str, path: str) -> str:
    """Build the ``code_uri`` a project version records for an archive.

    Parameters
    ----------
    storage_root
        The environment's storage root.
    path
        The archive's root-relative path.

    Returns
    -------
    str
        The URI, built as `lakana_worker.deploy.run_deploy` builds it.
    """
    return f"{storage_root.rstrip('/')}/{path}"


def _identity(path: str) -> tuple[str, str]:
    """Split an archive's path into the commit and the digest its name carries.

    Parameters
    ----------
    path
        A path that fully matches `_ARCHIVE`.

    Returns
    -------
    tuple of (str, str)
        The 40-hex commit and the 64-hex SHA-256.
    """
    commit, digest = path.rsplit("/", 1)[1].removesuffix(".tar.gz").split("-")
    return commit, digest


def _named_condition(uri: str, path: str) -> ColumnElement[bool]:
    """Build the condition under which a project version names an archive.

    Parameters
    ----------
    uri
        The archive's URI as a deploy would record it.
    path
        The archive's path, whose name holds the commit and the digest.

    Returns
    -------
    ColumnElement
        True for a row with that ``code_uri``, or with that commit and that digest
        whatever root its ``code_uri`` spells: the digest makes the name unique by content,
        so this survives two spellings of one location at the cost of an occasional orphan.
    """
    commit, digest = _identity(path)
    return or_(
        JobVersion.code_uri == uri,
        and_(JobVersion.commit_sha == commit, JobVersion.code_sha256 == digest),
    )


class _Root:
    """The state of one run over one storage root.

    Parameters
    ----------
    session
        Database session; every step ends with a rollback, nothing stays open.
    storage
        The root's storage.
    root
        The root without a trailing ``/``.
    environment_ids
        The environments that use the root.
    now
        The run's time.
    tick
        Raises `_Spent` when the deadline has passed.
    offset
        Where in the candidates to start.
    """

    def __init__(
        self,
        session: Session,
        storage: Storage,
        root: str,
        environment_ids: list[uuid.UUID],
        now: datetime,
        tick: Callable[[], None],
        offset: int,
    ) -> None:
        self._session = session
        self._storage = storage
        self._root = root
        self._ids = environment_ids
        self._now = now
        self._tick = tick
        self._offset = offset
        self.deleted = 0
        self.freed = 0
        self.left_alone = 0
        self.failures = 0

    def run(self) -> None:
        """Sweep the root once.

        Raises
        ------
        _Spent
            When the deadline passed.
        _Busy
            When a deploy is queued or running.
        """
        if _active_deploy(self._session, self._ids):
            self._session.rollback()
            raise _Busy
        self._session.rollback()
        cutoff = self._now - ORPHAN_BLOB_GRACE
        old: list[tuple[str, int]] = []
        for found in self._storage.list_objects(CODE_PREFIX):
            self._tick()
            if _ARCHIVE.fullmatch(found.path) is None:
                self.left_alone += 1
            elif found.modified_at is not None and found.modified_at < cutoff:
                old.append((found.path, found.size))
        if self.left_alone:
            logger.debug(
                "Code archive sweep: %d object(s) under %s are not code archives and are left "
                "alone.",
                self.left_alone,
                CODE_PREFIX,
            )
        old.sort()
        if old:
            start = self._offset % len(old)
            old = old[start:] + old[:start]
        for begin in range(0, len(old), _BATCH):
            self._batch(old[begin : begin + _BATCH])

    def _batch(self, batch: list[tuple[str, int]]) -> None:
        """Delete the archives of a batch that nothing names.

        Parameters
        ----------
        batch
            Path and size of old archives.

        Raises
        ------
        _Spent
            When the deadline passed.
        _Busy
            When a deploy is queued or running.
        """
        uris = {_code_uri(self._root, path): (path, size) for path, size in batch}
        pairs = {uri: _identity(path) for uri, (path, _) in uris.items()}
        rows = self._session.execute(
            select(JobVersion.code_uri, JobVersion.commit_sha, JobVersion.code_sha256).where(
                or_(
                    JobVersion.code_uri.in_(uris),
                    and_(
                        JobVersion.commit_sha.in_({p[0] for p in pairs.values()}),
                        JobVersion.code_sha256.in_({p[1] for p in pairs.values()}),
                    ),
                )
            )
        ).all()
        self._session.rollback()
        named_uris = {row[0] for row in rows}
        named_pairs = {(row[1], row[2]) for row in rows}
        for uri, (path, size) in uris.items():
            self._tick()
            if uri not in named_uris and pairs[uri] not in named_pairs:
                self._delete(uri, path, size)

    def _delete(self, uri: str, path: str, size: int) -> None:
        """Delete one archive after checking again that nothing needs it.

        Parameters
        ----------
        uri
            The archive's URI.
        path
            Its root-relative path.
        size
            Its size in bytes.

        Raises
        ------
        _Busy
            When a deploy is queued or running; nothing more is deleted in this root.
        """
        try:
            # In this order: a deploy records its project version and ends its run in one commit.
            if _active_deploy(self._session, self._ids):
                raise _Busy
            if self._session.scalar(
                select(JobVersion.id).where(_named_condition(uri, path)).limit(1)
            ):
                return
            self._storage.delete(path)
            self.deleted += 1
            self.freed += size
            self.failures = 0
        except _Busy:
            raise
        except Exception as exc:
            self.failures += 1
            logger.warning(
                "Code archive sweep: could not remove %s (%s).", shown(path), type(exc).__name__
            )
            if self.failures >= MAX_CONSECUTIVE_FAILURES:
                raise
        finally:
            self._session.rollback()


def sweep_code_archives(
    session: Session,
    storage: StorageResolver,
    *,
    now: datetime,
    max_seconds: float = CODE_SWEEP_MAX_SECONDS,
    monotonic: Callable[[], float] | None = None,
    rotation: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> CodeSweepReport:
    """Remove the code archives no project version names, in every storage root, once.

    Environments with the same storage root are one root: they share its objects.

    Parameters
    ----------
    session
        Database session; the sweep leaves it without a transaction open.
    storage
        Resolves each environment's storage.
    now
        The run's time (the database's clock in production).
    max_seconds
        How long the run may take, shared between the roots, before it stops at the next
        decision.
    monotonic
        The clock the deadline is read from; `time.monotonic` when `None`.
    rotation
        A number that grows by one with each run; it moves the starting root and archive so
        that no one's backlog always comes first.
    should_stop
        Called before every decision; the run ends when it returns `True` (shutdown).

    Returns
    -------
    CodeSweepReport
        What was removed. A root that failed or was busy is counted and skipped, the others
        are still swept.
    """
    clock = monotonic or time.monotonic
    stop = should_stop or (lambda: False)
    final = clock() + max_seconds
    rows = session.execute(
        select(Environment.id, Environment.name, Environment.storage_root).order_by(
            Environment.name
        )
    ).all()
    session.rollback()
    groups: dict[str, list[tuple[uuid.UUID, str]]] = defaultdict(list)
    for environment_id, name, root in rows:
        groups[root.rstrip("/")].append((environment_id, name))
    roots = sorted(groups)
    if roots:
        start = rotation % len(roots)
        roots = roots[start:] + roots[:start]
    deleted = freed = left_alone = failed = busy = 0
    stopped = False
    for position, root in enumerate(roots):
        moment = clock()
        if stop() or moment >= final:
            stopped = True
            break
        share = moment + (final - moment) / (len(roots) - position)

        def tick(limit: float = share) -> None:
            if stop() or clock() >= limit:
                raise _Spent

        members = groups[root]
        run: _Root | None = None
        try:
            backend = storage.for_environment(members[0][1]).storage
            run = _Root(session, backend, root, [m[0] for m in members], now, tick, rotation)
            run.run()
        except _Spent:
            stopped = True
        except _Busy:
            busy += 1
        except Exception as exc:
            failed += 1
            session.rollback()
            logger.warning(
                "The code archive sweep skipped the storage of environment %s (%s).",
                shown(members[0][1]),
                type(exc).__name__,
            )
        finally:
            if run is not None:
                deleted += run.deleted
                freed += run.freed
                left_alone += run.left_alone
    report = CodeSweepReport(
        deleted=deleted,
        freed_bytes=freed,
        left_alone=left_alone,
        failed_roots=failed,
        busy_roots=busy,
        stopped_early=stopped,
    )
    if deleted or failed or busy:
        logger.info(
            "Code archive sweep: %d unreferenced code archive(s) removed (%d bytes); %d object(s) "
            "left alone, %d storage root(s) failed, %d busy%s.",
            deleted,
            freed,
            left_alone,
            failed,
            busy,
            "; stopped at its time limit" if stopped else "",
        )
    return report
