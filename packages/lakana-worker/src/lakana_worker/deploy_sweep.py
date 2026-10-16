"""Removal of deploy checkouts a killed worker left behind.

A deploy fetches the commit into ``<temp>/lakana-deploy-<8 characters>`` and
removes it when the job ends. A worker that is killed mid-deploy never gets
there. `DeploySweeper` removes such leftovers with the `AttemptSweeper` design,
and is as narrow as it:

- it lists only the direct children of the temp directory, and removes only a
  real directory (never a symlink), owned by the worker's user, whose name is
  one ``tempfile.mkdtemp(prefix="lakana-deploy-")`` produces; any other name,
  ``lakana-spill-*`` and ``attempt-*`` included, is not looked at;
- a deploy holds a shared `flock` on its checkout (`hold_checkout`) from creation
  until its own removal; the sweeper removes a directory only while holding an
  exclusive, non-blocking one, and only if the path still is the inode it
  locked (`lock_exclusive_if_same`). The lock dies with the process, and works
  between containers that share a temp directory but not a PID namespace;
- age is the second guard: nothing younger than `MIN_AGE_SECONDS` (and than the
  deploy job's time limit) is touched. It covers a worker running older code that
  holds no lock, and the instant between ``mkdtemp`` and the ``flock``;
- every rule (a directory, owned by the worker's user, old enough) is decided on the
  descriptor that was opened with ``O_NOFOLLOW``, never on the path looked at before;
- a directory whose lock cannot be taken for any reason but "somebody holds it"
  (``EWOULDBLOCK``/``EAGAIN``), for instance on a filesystem without ``flock``, is kept at
  any age, and logged once per directory and process.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Generator
from pathlib import Path

from lakana_core.identifiers import shown
from lakana_worker.attempt_sweep import lock_exclusive_if_same
from lakana_worker.processes import remove_private_tree

logger = logging.getLogger(__name__)

CHECKOUT_PREFIX = "lakana-deploy-"
"""What `tempfile.mkdtemp` is given as prefix for a deploy checkout."""

MIN_AGE_SECONDS = 24 * 3600.0
"""Least age, in seconds, of an unlocked checkout before it is removed (24 hours).

Far above the deploy job time limit (`DeployLimits.job_seconds`, 10 minutes by default).
"""

_CHECKOUT_NAME = re.compile(re.escape(CHECKOUT_PREFIX) + r"[a-z0-9_]{8}")
"""A checkout's name: the prefix and the eight lowercase letters, digits or ``_`` mkdtemp adds."""

MAX_WARNED = 1000
"""Most directories a sweeper warns about once; further ones are kept without a message."""

_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@contextlib.contextmanager
def hold_checkout(path: Path) -> Generator[None]:
    """Hold a shared lock on a checkout directory for as long as the deploy uses it.

    A lock that cannot be taken (a filesystem without ``flock``) is logged and the
    deploy goes on: the sweeper cannot lock such a directory either, so it never removes
    it; if the worker is killed, that checkout stays until somebody removes it by hand.

    Parameters
    ----------
    path
        The checkout, just created.

    Yields
    ------
    None
        While the lock is held.
    """
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _OPEN_FLAGS)
        fcntl.flock(descriptor, fcntl.LOCK_SH)
    except OSError as exc:
        logger.warning(
            "The deploy checkout %s could not be locked (%s); the sweep never removes a directory "
            "it cannot lock, so if this worker is killed the checkout stays until you remove it.",
            path,
            exc.strerror or "filesystem error",
        )
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)


class DeploySweeper:
    """Remove old deploy checkouts nobody holds locked, at most once per interval.

    Parameters
    ----------
    temp
        The temp directory the checkouts are made in; `tempfile.gettempdir()` by default.
    max_age
        Seconds a checkout must have gone unchanged before it is removed; raised to
        `MIN_AGE_SECONDS` if smaller.
    interval
        Seconds between sweeps; 0 disables `maybe_sweep`.
    clock
        Wall-clock time in seconds; compared with directory mtimes.
    monotonic
        Monotonic time in seconds; paces `maybe_sweep`.
    """

    def __init__(
        self,
        temp: Path | None = None,
        *,
        max_age: float = MIN_AGE_SECONDS,
        interval: float = 3600.0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._temp = temp
        self._max_age = max(max_age, MIN_AGE_SECONDS)
        self._interval = interval
        self._clock = clock
        self._monotonic = monotonic
        self._last: float | None = None
        self._warned: set[str] = set()

    def maybe_sweep(self) -> None:
        """Sweep if none has run for `interval` seconds; the first call sweeps at once."""
        if self._interval <= 0:
            return
        now = self._monotonic()
        if self._last is not None and now - self._last < self._interval:
            return
        self.sweep()

    def sweep(self) -> int:
        """Remove every old, unheld deploy checkout.

        Returns
        -------
        int
            How many directories were removed. One log line gives the count.
        """
        self._last = self._monotonic()
        base = Path(tempfile.gettempdir()) if self._temp is None else self._temp
        try:
            names = os.listdir(base)
        except OSError as exc:
            logger.warning(
                "The temp directory %s could not be listed (%s); deploy checkouts are not swept.",
                base,
                exc.strerror or "filesystem error",
            )
            return 0
        removed = sum(
            self._sweep_entry(base / name)
            for name in sorted(names)
            if _CHECKOUT_NAME.fullmatch(name) is not None
        )
        if removed:
            logger.info("Removed %d deploy checkout(s) left by a worker that stopped.", removed)
        return removed

    def _sweep_entry(self, path: Path) -> int:
        """Remove one checkout if it is an old, unheld directory of this worker's user.

        Parameters
        ----------
        path
            A direct child of the temp directory whose name matches `_CHECKOUT_NAME`.

        Returns
        -------
        int
            1 if it was removed, else 0.
        """
        try:
            descriptor = os.open(path, _OPEN_FLAGS)
        except OSError:
            return 0
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                return 0
            if self._clock() - info.st_mtime <= self._max_age:
                return 0
            try:
                if not lock_exclusive_if_same(descriptor, path):
                    return 0
            except OSError as exc:
                if path.name not in self._warned and len(self._warned) < MAX_WARNED:
                    self._warned.add(path.name)
                    logger.warning(
                        "The lock on deploy checkout %s could not be taken (%s); a directory "
                        "that cannot be locked is kept, whatever its age.",
                        shown(str(path)),
                        exc.strerror or "filesystem error",
                    )
                return 0
            return 1 if remove_private_tree(path) else 0
        except OSError:
            return 0
        finally:
            os.close(descriptor)


def sweep_deploy_checkouts(temp: Path | None = None, *, max_age: float = MIN_AGE_SECONDS) -> int:
    """Sweep the deploy checkouts once, as the worker does at start-up.

    Parameters
    ----------
    temp
        The temp directory; `tempfile.gettempdir()` by default.
    max_age
        The deploy job's time limit in seconds; the age guard is the larger of this and
        `MIN_AGE_SECONDS`.

    Returns
    -------
    int
        How many directories were removed.
    """
    return DeploySweeper(temp, max_age=max_age).sweep()
