"""The worker's own caches: how much disk they take, and the trim of uv's package cache.

Every worker measures its caches in its housekeeping, every `LAKANA_RUN__SWEEP_INTERVAL`, in a
thread of its own (`CacheKeeper`), and hands the figures to its heartbeat, which stores them
with the worker's row; the server shows them (`lakana worker list`, `lakana env usage`) without
reaching into a worker. Three directories are measured, as ``du`` counts them (allocated
blocks, links not followed): the Python environments of projects, uv's package download cache
and the working directories of the Python tasks. uv links an environment's files to its cache
(hard links), so a file both hold is counted once, under the environments: the package figure
is what removing the cache would free at most. A measurement has a time limit
(`MEASURE_MAX_SECONDS`); one cut off by it is stored as incomplete, a floor.

Above `LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES` the keeper trims the package cache, and only it:
the environments are evicted by their own count (`LAKANA_RUN__VENV_KEEP`), the task directories
by the attempt sweep. The trim deletes nothing itself; it asks uv to, and it is cautious:

- it starts only while this worker builds no environment (`builds_in_progress`), and checks
  again before each step;
- each step is a ``uv cache`` command, which takes uv's own lock of the cache: while any uv
  process uses the cache (a build of another worker that shares the volume), uv says the cache
  is in use and waits. The trim then ends the command at once, before it removed anything, and
  tries again at the next sweep: it never waits for, nor interrupts, a build;
- first ``uv cache prune``: what no package entry refers to any more. Then, while the cache is
  still above the limit, ``uv cache clean`` of the packages whose entries changed longest ago, a
  few at a time, never one changed within `LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS`;
- an environment that exists keeps working: its files are hard links or copies of the cache's,
  never symbolic links (the build runs uv without configuration, `lakana_worker.venvs`), so
  removing a cache entry frees its blocks only once no environment holds them. A removed
  package is downloaded again by the next build that needs it;
- the whole trim has a time limit (`TRIM_MAX_SECONDS`); a command still running at it is ended,
  and the outcome is logged with ``event=package_cache_over_limit``.

Package names are read from the cache's own directories and passed to uv as arguments only when
they are normalised package names; nothing is run through a shell.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import selectors
import signal
import stat
import subprocess  # nosec B404 -- uv is invoked with fixed argv, no shell
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from lakana_core.log_events import LogEvent, log_event
from lakana_core.runs.workers import CacheSizes
from lakana_worker.processes import signal_group
from lakana_worker.venvs import UV_LAUNCHER, builds_in_progress

logger = logging.getLogger(__name__)

MEASURE_MAX_SECONDS = 60.0
"""How long one measurement of the three directories may take."""

TRIM_MAX_SECONDS = 300.0
"""How long one trim of the package cache may take, its uv commands included."""

CLEAN_BATCH = 20
"""How many packages one ``uv cache clean`` removes before the cache is measured again."""

_IN_USE = b"is currently in-use"
"""What uv writes when another uv process holds the cache ("Cache is currently in-use, waiting
for other uv processes to finish"). Should the wording change, the time limit ends the wait."""

_BUCKET = re.compile(r"(?:wheels|sdists)-v[0-9]+")
"""uv's per-package buckets: ``wheels-v6/pypi/<name>``, ``sdists-v9/index/<hash>/<name>``."""

_PACKAGE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,198}[a-z0-9])?")
"""A normalised package name: never starts with ``-``, so it is never read as an option."""

_TAIL_BYTES = 4096


@dataclass(frozen=True)
class CachePaths:
    """The three cache directories of a worker.

    Parameters
    ----------
    environments
        The Python environments of projects (`LAKANA_RUN__VENV_CACHE` or ``<cache>/venvs``).
    packages
        uv's package download cache (``<cache>/uv``).
    tasks
        The working directories of the Python tasks (``<cache>/attempts``).
    """

    environments: Path
    packages: Path
    tasks: Path


def _on_disk(info: os.stat_result) -> int:
    """Read how much disk an entry takes.

    Parameters
    ----------
    info
        Its `lstat`.

    Returns
    -------
    int
        Its allocated blocks in bytes, as ``du`` counts them; its size where the platform has no
        block count.
    """
    blocks = getattr(info, "st_blocks", None)
    return info.st_size if blocks is None else blocks * 512


def _walk(
    path: Path, seen: set[tuple[int, int]], deadline: float, clock: Callable[[], float]
) -> tuple[int, bool]:
    """Add up the disk a directory tree takes, without following a link.

    Parameters
    ----------
    path
        The directory; a missing or unreadable one takes nothing.
    seen
        Files with more than one link already counted, by device and inode; extended here, so a
        file is counted once over several trees.
    deadline
        The `clock` value at which the walk stops.
    clock
        Monotonic seconds.

    Returns
    -------
    tuple of int and bool
        The bytes, and whether the walk finished before `deadline`.
    """
    total = 0
    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if clock() >= deadline:
                    return total, False
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif info.st_nlink > 1:
                    key = (info.st_dev, info.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                total += _on_disk(info)
    return total, True


def measure(
    paths: CachePaths,
    *,
    max_seconds: float = MEASURE_MAX_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> CacheSizes:
    """Measure the three cache directories, within a time limit.

    Parameters
    ----------
    paths
        The directories.
    max_seconds
        The time the three walks may take together.
    clock
        Monotonic seconds; a test replaces it.

    Returns
    -------
    CacheSizes
        The environments first, so that a file the package cache shares with an environment is
        counted under the environments; `complete` is `False` when the limit cut a walk off, and
        a directory not reached then counts 0.
    """
    deadline = clock() + max_seconds
    seen: set[tuple[int, int]] = set()
    sizes: list[int] = []
    complete = True
    for path in (paths.environments, paths.packages, paths.tasks):
        if not complete:
            sizes.append(0)
            continue
        size, complete = _walk(path, seen, deadline, clock)
        sizes.append(size)
    return CacheSizes(
        environments_bytes=sizes[0],
        packages_bytes=sizes[1],
        tasks_bytes=sizes[2],
        complete=complete,
    )


def _newest_change(path: Path) -> float:
    """Read when a package's cache directory or one of its entries last changed.

    Parameters
    ----------
    path
        The package's directory in a bucket.

    Returns
    -------
    float
        The newest modification time, links not followed; `inf` when it cannot be read, so that
        such a package is never chosen.
    """
    try:
        newest = path.lstat().st_mtime
        with os.scandir(path) as entries:
            for entry in entries:
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    except OSError:
        return float("inf")
    return newest


def _package_directories(cache: Path) -> dict[str, float]:
    """Find the packages the cache holds, each with when it last changed.

    Parameters
    ----------
    cache
        uv's cache directory.

    Returns
    -------
    dict of str to float
        Normalised name to the newest change of any of its directories, over every bucket and
        index; names that are not normalised package names are left out.
    """
    found: dict[str, float] = {}
    try:
        buckets = [entry for entry in os.scandir(cache) if _BUCKET.fullmatch(entry.name)]
    except OSError:
        return found
    for bucket in buckets:
        if not bucket.is_dir(follow_symlinks=False):
            continue
        parents = [Path(bucket.path) / "pypi"]
        with contextlib.suppress(OSError), os.scandir(Path(bucket.path) / "index") as indexes:
            parents += [
                Path(index.path) for index in indexes if index.is_dir(follow_symlinks=False)
            ]
        for parent in parents:
            with contextlib.suppress(OSError), os.scandir(parent) as packages:
                for package in packages:
                    if _PACKAGE.fullmatch(package.name) and package.is_dir(follow_symlinks=False):
                        changed = _newest_change(Path(package.path))
                        found[package.name] = max(found.get(package.name, changed), changed)
    return found


@dataclass(frozen=True)
class TrimSettings:
    """What the trim needs.

    Parameters
    ----------
    uv
        The `uv` executable, an absolute path.
    max_bytes
        `LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES`; 0 never trims.
    min_age_seconds
        `LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS`, in seconds.
    max_seconds
        The time one trim may take.
    """

    uv: str
    max_bytes: int
    min_age_seconds: float
    max_seconds: float = TRIM_MAX_SECONDS


class _Busy(Exception):
    """The cache is in use: by a build of this worker, or by another uv process."""


class _Failed(Exception):
    """A uv command failed or ran into the trim's time limit; the text says which."""


class PackageCacheTrim:
    """Bring uv's package cache below its limit, cautiously (see the module's description).

    Parameters
    ----------
    paths
        The cache directories; only `packages` is trimmed, all three are measured.
    settings
        The limit, the minimum age and uv.
    clock
        Monotonic seconds, for the time limit.
    wall_clock
        Seconds since the epoch, compared with the cache's modification times.
    measure_sizes
        Measures the caches; `measure` over `paths` by default.
    """

    def __init__(
        self,
        paths: CachePaths,
        settings: TrimSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        measure_sizes: Callable[[], CacheSizes] | None = None,
    ) -> None:
        self._paths = paths
        self._settings = settings
        self._clock = clock
        self._wall_clock = wall_clock
        self._measure = measure_sizes or (lambda: measure(paths))

    def trim(self, sizes: CacheSizes, stop: threading.Event | None = None) -> CacheSizes:
        """Trim the package cache if it is above the limit, and say how it went in the log.

        Parameters
        ----------
        sizes
            The caches as just measured.
        stop
            Set at shutdown; no further uv command starts once it is.

        Returns
        -------
        CacheSizes
            The caches after the trim; `sizes` when nothing ran.
        """
        limit = self._settings.max_bytes
        # A measurement cut off by its time limit is a floor: above the limit, the cache is too.
        if limit <= 0 or sizes.packages_bytes <= limit:
            return sizes
        before = sizes.packages_bytes
        deadline = self._clock() + self._settings.max_seconds
        tried: set[str] = set()
        reason = "the cache is still too large"
        try:
            self._uv(["prune"], deadline, stop)
            sizes = self._measure()
            while sizes.packages_bytes > limit:
                batch, left = self._oldest(tried)
                if not batch:
                    reason = (
                        "every package left was downloaded within "
                        "LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS"
                        if left
                        else "no package is left to remove; what remains is uv's own data"
                    )
                    break
                tried.update(batch)
                self._uv(["clean", *batch], deadline, stop)
                sizes = self._measure()
        except _Busy as exc:
            logger.info(
                "The package cache (%d bytes, above LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES=%d) "
                "was not trimmed now: %s. It is tried again at the next sweep.",
                before,
                limit,
                exc,
            )
            return sizes
        except _Failed as exc:
            reason = str(exc)
        if sizes.packages_bytes > limit:
            logger.warning(
                "The package cache is %d bytes after a trim, above "
                "LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES=%d (%s). Raise the limit, give the "
                "worker's cache volume more space, or lower "
                "LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS.",
                sizes.packages_bytes,
                limit,
                reason,
                extra=log_event(LogEvent.PACKAGE_CACHE_OVER_LIMIT),
            )
        else:
            logger.info(
                "The package cache was trimmed from %d to %d bytes "
                "(LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES=%d).",
                before,
                sizes.packages_bytes,
                limit,
                extra=log_event(LogEvent.PACKAGE_CACHE_TRIMMED),
            )
        return sizes

    def _oldest(self, tried: set[str]) -> tuple[list[str], bool]:
        """Choose the next packages to remove.

        Parameters
        ----------
        tried
            Packages already removed in this trim, never chosen again.

        Returns
        -------
        tuple of list of str and bool
            Up to `CLEAN_BATCH` names, oldest change first, none changed within the minimum age;
            and whether any package not removed yet is left at all, young or old.
        """
        cutoff = self._wall_clock() - self._settings.min_age_seconds
        left = {
            name: changed
            for name, changed in _package_directories(self._paths.packages).items()
            if name not in tried
        }
        old = sorted((changed, name) for name, changed in left.items() if changed <= cutoff)
        return [name for _, name in old[:CLEAN_BATCH]], bool(left)

    def _uv(self, arguments: Sequence[str], deadline: float, stop: threading.Event | None) -> None:
        """Run one ``uv cache`` command, giving up as soon as the cache turns out to be in use.

        Parameters
        ----------
        arguments
            What follows ``uv cache``: ``prune``, or ``clean`` and package names.
        deadline
            The `clock` value at which the command is ended.
        stop
            Set at shutdown; checked before the command starts.

        Raises
        ------
        _Busy
            If this worker builds an environment, or uv says another process uses the cache;
            nothing was removed.
        _Failed
            If the worker is stopping, uv could not be started, exited with an error, or ran
            into `deadline`.
        """
        if stop is not None and stop.is_set():
            raise _Failed("the worker is stopping")
        if builds_in_progress():
            raise _Busy("this worker is building a Python environment")
        if self._clock() >= deadline:
            raise _Failed("the trim ran out of time")
        command, *rest = arguments
        cache = self._paths.packages
        argv = [self._settings.uv, "cache", command, "--no-config", "--cache-dir", str(cache)]
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            # uv refuses every command started inside its own cache ("The project directory is
            # inside the cache directory"), so it runs from the root, which no cache contains.
            "HOME": os.sep,
            "UV_NO_PROGRESS": "1",
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
        try:
            process = subprocess.Popen(  # nosec B603 -- fixed argv, no shell, minimal env
                [sys.executable, "-I", "-c", UV_LAUNCHER, str(os.getpid()), *argv, *rest],
                cwd=os.sep,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise _Failed(f"uv could not be started ({exc.strerror or exc})") from exc
        self._watch(process, command, deadline)

    def _watch(self, process: subprocess.Popen[bytes], command: str, deadline: float) -> None:
        """Read a uv command's output until it ends, the cache is in use, or time runs out.

        Parameters
        ----------
        process
            The started command.
        command
            ``prune`` or ``clean``, for the message.
        deadline
            The `clock` value at which it is ended.

        Raises
        ------
        _Busy
            If uv says another process uses the cache.
        _Failed
            If it exited with an error or ran into `deadline`.
        """
        pipe = process.stdout
        assert pipe is not None  # nosec B101 -- stdout is PIPE
        tail = b""
        selector = selectors.DefaultSelector()
        selector.register(pipe, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise _Failed(f"`uv cache {command}` ran out of the trim's time and was ended")
                if not selector.select(min(remaining, 1.0)):
                    continue
                chunk = os.read(pipe.fileno(), 65536)
                if not chunk:
                    break
                tail = (tail + chunk)[-_TAIL_BYTES:]
                if _IN_USE in tail:
                    raise _Busy("another uv process, a build of a worker that shares it, uses it")
        finally:
            selector.close()
            if process.poll() is None:
                signal_group(process, signal.SIGKILL)
            process.wait()
            pipe.close()
        if process.returncode != 0:
            raise _Failed(f"`uv cache {command}` exited with status {process.returncode}")


class CacheKeeper:
    """Measure the caches, trim the package cache, and keep the latest figures for the heartbeat.

    Parameters
    ----------
    paths
        The cache directories.
    interval
        Seconds between two runs (`LAKANA_RUN__SWEEP_INTERVAL`); 0 never runs.
    trim
        The trim of the package cache; `None` measures only (no uv, or the limit is 0).
    stop
        Set at shutdown; a running trim starts no further uv command.
    monotonic
        Paces the runs; a test replaces it.
    measure_sizes
        Measures the caches; `measure` over `paths` by default.
    """

    def __init__(
        self,
        paths: CachePaths,
        *,
        interval: float,
        trim: PackageCacheTrim | None = None,
        stop: threading.Event | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        measure_sizes: Callable[[], CacheSizes] | None = None,
    ) -> None:
        self._interval = interval
        self._trim = trim
        self._stop = stop
        self._monotonic = monotonic
        self._measure = measure_sizes or (lambda: measure(paths))
        self._last: float | None = None
        self._thread: threading.Thread | None = None
        self._latest: CacheSizes | None = None
        self._lock = threading.Lock()

    def latest(self) -> CacheSizes | None:
        """Return the figures of the last run.

        Returns
        -------
        CacheSizes or None
            `None` until the first run has ended.
        """
        with self._lock:
            return self._latest

    def maybe_start(self) -> threading.Thread | None:
        """Start a run in a thread of its own if one is due and none is running.

        The first call starts one at once; afterwards one every `interval` seconds, counted from
        the start of the last.

        Returns
        -------
        threading.Thread or None
            The started thread; `None` when nothing started.
        """
        if self._interval <= 0:
            return None
        now = self._monotonic()
        running = self._thread is not None and self._thread.is_alive()
        if running or (self._last is not None and now - self._last < self._interval):
            return None
        self._last = now
        self._thread = threading.Thread(target=self.run, name="lakana-caches", daemon=True)
        self._thread.start()
        return self._thread

    def run(self) -> None:
        """Measure, trim when needed, and keep the result; a failure is only logged."""
        try:
            sizes = self._measure()
            if self._trim is not None:
                sizes = self._trim.trim(sizes, self._stop)
        except Exception as exc:
            logger.warning(
                "Measuring the worker's caches failed (%s); they are measured again at the next "
                "sweep.",
                type(exc).__name__,
                exc_info=True,
                extra=log_event(LogEvent.SWEEP_FAILED),
            )
            return
        if not sizes.complete:
            logger.info(
                "Measuring the worker's caches stopped at its time limit (%g s); the figures "
                "shown are a floor.",
                MEASURE_MAX_SECONDS,
            )
        with self._lock:
            self._latest = sizes
