"""The worker's caches: measured within a time limit, and uv's package cache trimmed with care.

uv is a script that records its calls and does what `behaviour.json` says; nothing reaches a
network and no real cache is touched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess  # nosec B404 -- the real uv with fixed argv, offline
import sys
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

import lakana_worker.caches as caches_module
from lakana_core.runs.workers import CacheSizes
from lakana_worker.caches import (
    CLEAN_BATCH,
    CacheKeeper,
    CachePaths,
    PackageCacheTrim,
    TrimSettings,
    measure,
)

_FAKE_UV = """#!{python}
import json, os, shutil, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
behaviour = json.loads((here / "behaviour.json").read_text())
with (here / "calls.jsonl").open("a") as handle:
    handle.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
command = sys.argv[2]
cache = Path(sys.argv[sys.argv.index("--cache-dir") + 1]).resolve()
here_now = Path.cwd().resolve()
if here_now == cache or cache in here_now.parents:
    # What a real uv does for any command started inside its own cache.
    print("error: The project directory '.' is inside the cache directory '.'", flush=True)
    sys.exit(2)
mode = behaviour.get(command, "ok")
if mode == "in-use":
    print("Cache is currently in-use, waiting for other uv processes to finish", flush=True)
    time.sleep(60)
if mode == "sleep":
    time.sleep(60)
if mode == "fail":
    sys.exit(2)
for name in sys.argv[sys.argv.index("--cache-dir") + 2:]:
    shutil.rmtree(cache / "wheels-v6" / "pypi" / name, ignore_errors=True)
print("Removed 3 files", flush=True)
"""

_DAY = 86_400.0
_NOW = 1_800_000_000.0


class FakeUv:
    """A scripted `uv`.

    Parameters
    ----------
    root
        A per-test directory.
    """

    def __init__(self, root: Path) -> None:
        self.directory = root / "fake-uv"
        self.directory.mkdir()
        self.path = self.directory / "uv"
        self.path.write_text(_FAKE_UV.format(python=sys.executable))
        self.path.chmod(0o755)
        self.behave()

    def behave(self, **behaviour: str) -> None:
        """Set what the next commands do.

        Parameters
        ----------
        **behaviour
            By command (``prune``, ``clean``): ``ok``, ``in-use``, ``sleep`` or ``fail``.
        """
        (self.directory / "behaviour.json").write_text(json.dumps(behaviour))

    def calls(self) -> list[dict]:
        """Return every recorded call.

        Returns
        -------
        list of dict
            ``argv`` and ``env`` per call.
        """
        log = self.directory / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def uv(tmp_path: Path) -> FakeUv:
    """Build the fake `uv`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    FakeUv
        The fake.
    """
    return FakeUv(tmp_path)


@pytest.fixture
def paths(tmp_path: Path) -> CachePaths:
    """Name the three cache directories under the test's directory; none exists yet.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    CachePaths
        ``venvs``, ``uv`` and ``attempts``.
    """
    return CachePaths(
        environments=tmp_path / "venvs", packages=tmp_path / "uv", tasks=tmp_path / "attempts"
    )


def _du(*files: Path) -> int:
    """Add up what files take on disk, as the measurement counts them.

    Parameters
    ----------
    *files
        The files and directories.

    Returns
    -------
    int
        Their allocated bytes.
    """
    return sum(caches_module._on_disk(path.lstat()) for path in files)


def _write(path: Path, size: int) -> Path:
    """Write a file of `size` bytes, its directories too.

    Parameters
    ----------
    path
        The file.
    size
        Its size.

    Returns
    -------
    Path
        `path`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _package(paths: CachePaths, name: str, age_days: float, *, index: str | None = None) -> Path:
    """Put a package's entry into uv's cache, last changed `age_days` ago.

    Parameters
    ----------
    paths
        The cache directories.
    name
        The package's directory name.
    age_days
        How long ago it changed, in days before `_NOW`.
    index
        The hash directory of a registered index; `None` puts it under ``pypi``.

    Returns
    -------
    Path
        The package's directory.
    """
    parent = paths.packages / "wheels-v6" / ("pypi" if index is None else f"index/{index}")
    directory = parent / name
    entry = _write(directory / "1.0-py3-none-any.http", 10)
    moment = _NOW - age_days * _DAY
    os.utime(entry, (moment, moment))
    os.utime(directory, (moment, moment))
    return directory


def _sizes(packages: int, *, complete: bool = True) -> CacheSizes:
    """Build measured sizes with a given package cache size.

    Parameters
    ----------
    packages
        The package cache's bytes.
    complete
        Whether the measurement finished.

    Returns
    -------
    CacheSizes
        With 1 byte of environments and none of tasks.
    """
    return CacheSizes(
        environments_bytes=1, packages_bytes=packages, tasks_bytes=0, complete=complete
    )


def _trim(
    paths: CachePaths,
    uv: FakeUv,
    measured: list[int],
    *,
    max_bytes: int = 100,
    min_age_days: float = 1.0,
    max_seconds: float = 30.0,
) -> PackageCacheTrim:
    """Build a trim whose measurements return the given package sizes in turn.

    Parameters
    ----------
    paths
        The cache directories.
    uv
        The fake uv.
    measured
        The package cache sizes the measurements after each step return; the last repeats.
    max_bytes
        The limit.
    min_age_days
        The minimum age, in days.
    max_seconds
        The trim's time limit.

    Returns
    -------
    PackageCacheTrim
        The trim.
    """
    remaining: Iterator[int] = iter(measured)
    last = [measured[-1]]

    def next_size() -> CacheSizes:
        """Return the next scripted measurement.

        Returns
        -------
        CacheSizes
            The next size, or the last one again.
        """
        last[0] = next(remaining, last[0])
        return _sizes(last[0])

    paths.packages.mkdir(parents=True, exist_ok=True)
    return PackageCacheTrim(
        paths,
        TrimSettings(
            uv=str(uv.path),
            max_bytes=max_bytes,
            min_age_seconds=min_age_days * _DAY,
            max_seconds=max_seconds,
        ),
        wall_clock=lambda: _NOW,
        measure_sizes=next_size,
    )


def test_missing_directories_take_nothing(paths: CachePaths) -> None:
    assert measure(paths) == CacheSizes(0, 0, 0, complete=True)


def test_each_directory_is_measured_and_a_shared_file_counts_once_under_the_environments(
    paths: CachePaths,
) -> None:
    environment = _write(paths.environments / "key" / "lib" / "a.py", 5000)
    cached = paths.packages / "archive-v0" / "x" / "a.py"
    cached.parent.mkdir(parents=True)
    os.link(environment, cached)
    own = _write(paths.packages / "archive-v0" / "y" / "b.py", 7000)
    (paths.packages / "wheels-v6").mkdir()
    (paths.packages / "wheels-v6" / "link").symlink_to(paths.packages / "archive-v0")
    task = _write(paths.tasks / "attempt-abcdefgh" / "out", 3000)

    found = measure(paths)

    assert found.environments_bytes == _du(
        paths.environments / "key", paths.environments / "key" / "lib", environment
    )
    assert found.packages_bytes == _du(
        paths.packages / "archive-v0",
        paths.packages / "archive-v0" / "x",
        paths.packages / "archive-v0" / "y",
        own,
        paths.packages / "wheels-v6",
        paths.packages / "wheels-v6" / "link",
    )
    assert found.tasks_bytes == _du(paths.tasks / "attempt-abcdefgh", task)
    assert found.complete


def test_a_measurement_stops_at_its_time_limit_and_says_so(paths: CachePaths) -> None:
    _write(paths.environments / "a", 10)
    _write(paths.packages / "b", 10)
    ticks = iter([0.0, 0.0, 10.0])
    found = measure(paths, max_seconds=5.0, clock=lambda: next(ticks, 10.0))
    assert found == CacheSizes(
        environments_bytes=_du(paths.environments / "a"),
        packages_bytes=0,
        tasks_bytes=0,
        complete=False,
    )


def test_an_entry_that_vanishes_during_the_walk_is_skipped(
    paths: CachePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(paths.tasks / "gone", 10)

    class Vanishing:
        """A directory entry whose file is gone."""

        path = str(paths.tasks / "gone")

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            """Fail as for a removed file.

            Parameters
            ----------
            follow_symlinks
                Ignored.

            Raises
            ------
            FileNotFoundError
                Always.
            """
            raise FileNotFoundError(self.path)

    class Listing:
        """What `os.scandir` returns, with one vanished entry."""

        def __enter__(self) -> Listing:
            return self

        def __iter__(self) -> Iterator[Vanishing]:
            return iter([Vanishing()])

        def __exit__(self, *exc: object) -> None:
            return None

    real = os.scandir
    monkeypatch.setattr(
        caches_module.os,
        "scandir",
        lambda path: (
            Listing() if not isinstance(path, int) and Path(path) == paths.tasks else real(path)
        ),
    )
    assert measure(paths).tasks_bytes == 0


def test_a_cache_below_its_limit_or_without_one_is_not_trimmed(
    paths: CachePaths, uv: FakeUv
) -> None:
    assert _trim(paths, uv, [50]).trim(_sizes(100)) == _sizes(100)
    assert _trim(paths, uv, [50], max_bytes=0).trim(_sizes(10**12)) == _sizes(10**12)
    assert uv.calls() == []


def test_a_prune_that_is_enough_is_all_that_runs(
    paths: CachePaths, uv: FakeUv, caplog: pytest.LogCaptureFixture
) -> None:
    _package(paths, "old", 30)
    with caplog.at_level(logging.INFO, logger=caches_module.__name__):
        after = _trim(paths, uv, [80]).trim(_sizes(150))
    assert after == _sizes(80)
    [call] = uv.calls()
    assert call["argv"] == ["cache", "prune", "--no-config", "--cache-dir", str(paths.packages)]
    assert call["env"]["UV_OFFLINE"] == "1"
    assert "UV_CACHE_DIR" not in call["env"]
    [record] = [r for r in caplog.records if "trimmed from 150 to 80" in r.getMessage()]
    assert getattr(record, "event", None) == "package_cache_trimmed"


def test_then_the_packages_changed_longest_ago_are_removed_and_never_a_young_one(
    paths: CachePaths, uv: FakeUv
) -> None:
    _package(paths, "oldest", 300)
    _package(paths, "older", 30, index="0123456789abcdef")
    _package(paths, "older", 40)
    _package(paths, "young", 0.5)
    _package(paths, "-rf", 999)
    _package(paths, "Not_Normalised", 999)
    (paths.packages / "wheels-v6" / "pypi" / "a-file").write_text("not a package")
    (paths.packages / "archive-v0").mkdir()
    (paths.packages / "sdists-v9").write_text("a file where a bucket would be")

    after = _trim(paths, uv, [150, 120, 90], max_bytes=100).trim(_sizes(200))

    assert after == _sizes(120)
    commands = [call["argv"][1:] for call in uv.calls()]
    removed = commands[1][4:]
    assert commands[0][0] == "prune"
    assert commands[1][:4] == ["clean", "--no-config", "--cache-dir", str(paths.packages)]
    assert removed == ["oldest", "older"]
    assert len(commands) == 2


def test_everything_left_being_young_is_a_warning_with_the_reason(
    paths: CachePaths, uv: FakeUv, caplog: pytest.LogCaptureFixture
) -> None:
    _package(paths, "young", 0.5)
    with caplog.at_level(logging.INFO, logger=caches_module.__name__):
        after = _trim(paths, uv, [150]).trim(_sizes(200))
    assert after == _sizes(150)
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert getattr(record, "event", None) == "package_cache_over_limit"
    assert "PACKAGE_CACHE_MIN_AGE_HOURS" in record.getMessage()
    assert "Raise the limit" in record.getMessage()


def test_a_cache_with_no_package_left_says_so_and_not_that_they_are_young(
    paths: CachePaths, uv: FakeUv, caplog: pytest.LogCaptureFixture
) -> None:
    (paths.packages / "archive-v0").mkdir(parents=True)
    with caplog.at_level(logging.WARNING, logger=caches_module.__name__):
        _trim(paths, uv, [150], min_age_days=0).trim(_sizes(200))
    [record] = caplog.records
    assert "no package is left to remove" in record.getMessage()
    assert "downloaded within" not in record.getMessage()


def test_uv_runs_from_outside_its_cache(paths: CachePaths, uv: FakeUv) -> None:
    """A real uv refuses every command started inside its own cache; the fake does the same."""
    _trim(paths, uv, [80]).trim(_sizes(150))
    [call] = uv.calls()
    assert call["env"]["HOME"] == os.sep
    assert call["argv"][:2] == ["cache", "prune"]


def test_more_old_packages_than_one_batch_are_removed_a_batch_at_a_time(
    paths: CachePaths, uv: FakeUv
) -> None:
    for number in range(CLEAN_BATCH + 3):
        _package(paths, f"p{number:03d}", 10 + number)
    _trim(paths, uv, [150, 150, 150, 50]).trim(_sizes(200))
    batches = [call["argv"][5:] for call in uv.calls() if call["argv"][1] == "clean"]
    assert [len(batch) for batch in batches] == [CLEAN_BATCH, 3]
    assert not set(batches[0]) & set(batches[1])


def test_a_cache_another_uv_process_uses_is_left_at_once_and_tried_again_later(
    paths: CachePaths, uv: FakeUv, caplog: pytest.LogCaptureFixture
) -> None:
    uv.behave(prune="in-use")
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger=caches_module.__name__):
        after = _trim(paths, uv, [50]).trim(_sizes(200))
    assert time.monotonic() - started < 20
    assert after == _sizes(200)
    assert "another uv process" in caplog.text
    assert "tried again at the next sweep" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_trim_starts_while_this_worker_builds_an_environment(
    paths: CachePaths, uv: FakeUv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(caches_module, "builds_in_progress", lambda: 1)
    with caplog.at_level(logging.INFO, logger=caches_module.__name__):
        assert _trim(paths, uv, [50]).trim(_sizes(200)) == _sizes(200)
    assert uv.calls() == []
    assert "this worker is building a Python environment" in caplog.text


@pytest.mark.parametrize(
    ("behaviour", "max_seconds", "reason"),
    [
        ({"prune": "fail"}, 30.0, "`uv cache prune` exited with status 2"),
        ({"prune": "sleep"}, 1.0, "`uv cache prune` ran out of the trim's time"),
    ],
    ids=["fails", "runs-too-long"],
)
def test_a_uv_command_that_fails_or_runs_too_long_is_a_warning(
    paths: CachePaths,
    uv: FakeUv,
    caplog: pytest.LogCaptureFixture,
    behaviour: dict[str, str],
    max_seconds: float,
    reason: str,
) -> None:
    uv.behave(**behaviour)
    with caplog.at_level(logging.WARNING, logger=caches_module.__name__):
        _trim(paths, uv, [150], max_seconds=max_seconds).trim(_sizes(200))
    assert reason in caplog.text


def test_a_uv_that_cannot_be_started_is_a_warning(
    paths: CachePaths, uv: FakeUv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        """Fail as a missing interpreter would.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        OSError
            Always.
        """
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(caches_module.subprocess, "Popen", refuse)
    with caplog.at_level(logging.WARNING, logger=caches_module.__name__):
        _trim(paths, uv, [150]).trim(_sizes(200))
    assert "uv could not be started (No such file or directory)" in caplog.text


def test_a_trim_out_of_time_or_at_shutdown_starts_no_command(
    paths: CachePaths, uv: FakeUv, caplog: pytest.LogCaptureFixture
) -> None:
    stop = threading.Event()
    stop.set()
    with caplog.at_level(logging.WARNING, logger=caches_module.__name__):
        _trim(paths, uv, [150]).trim(_sizes(200), stop)
        _trim(paths, uv, [150], max_seconds=0.0).trim(_sizes(200))
    assert uv.calls() == []
    assert "the worker is stopping" in caplog.text
    assert "the trim ran out of time" in caplog.text


def test_a_package_directory_that_cannot_be_read_is_never_chosen(
    paths: CachePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _package(paths, "locked", 300)
    real = os.scandir
    monkeypatch.setattr(
        caches_module.os,
        "scandir",
        lambda path: (
            (_ for _ in ()).throw(PermissionError())
            if not isinstance(path, int) and Path(path) == directory
            else real(path)
        ),
    )
    assert caches_module._package_directories(paths.packages) == {"locked": float("inf")}
    assert caches_module._package_directories(paths.packages / "missing") == {}


def test_the_keeper_runs_at_once_then_once_per_interval(paths: CachePaths) -> None:
    now = [0.0]
    measured = [_sizes(5)]
    keeper = CacheKeeper(
        paths, interval=3600, monotonic=lambda: now[0], measure_sizes=lambda: measured[0]
    )
    assert keeper.latest() is None
    first = keeper.maybe_start()
    assert first is not None
    first.join(10)
    assert keeper.latest() == _sizes(5)
    now[0] = 3599.0
    assert keeper.maybe_start() is None
    now[0] = 3600.0
    measured[0] = _sizes(6)
    second = keeper.maybe_start()
    assert second is not None
    second.join(10)
    assert keeper.latest() == _sizes(6)
    assert CacheKeeper(paths, interval=0).maybe_start() is None


def test_the_keeper_starts_no_second_run_while_one_runs(paths: CachePaths) -> None:
    release = threading.Event()

    def slow() -> CacheSizes:
        """Wait until released.

        Returns
        -------
        CacheSizes
            One byte of packages.
        """
        release.wait(10)
        return _sizes(1)

    keeper = CacheKeeper(paths, interval=1, monotonic=lambda: 1e9, measure_sizes=slow)
    keeper._last = 0.0
    running = keeper.maybe_start()
    assert running is not None
    keeper._last = 0.0
    assert keeper.maybe_start() is None
    release.set()
    running.join(10)


def test_the_keeper_trims_after_measuring(paths: CachePaths) -> None:
    class Trim:
        """Records what it was given."""

        def __init__(self) -> None:
            self.given: list[CacheSizes] = []

        def trim(self, sizes: CacheSizes, stop: threading.Event | None) -> CacheSizes:
            """Record and shrink.

            Parameters
            ----------
            sizes
                As measured.
            stop
                Ignored.

            Returns
            -------
            CacheSizes
                Smaller.
            """
            self.given.append(sizes)
            return _sizes(1)

    trim = Trim()
    keeper = CacheKeeper(
        paths,
        interval=1,
        trim=cast(PackageCacheTrim, trim),
        measure_sizes=lambda: _sizes(9),
    )
    keeper.run()
    assert trim.given == [_sizes(9)]
    assert keeper.latest() == _sizes(1)


def test_a_failed_or_cut_off_measurement_is_logged(
    paths: CachePaths, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> CacheSizes:
        """Fail.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("boom")

    with caplog.at_level(logging.INFO, logger=caches_module.__name__):
        failing = CacheKeeper(paths, interval=1, measure_sizes=broken)
        failing.run()
        cut = CacheKeeper(paths, interval=1, measure_sizes=lambda: _sizes(3, complete=False))
        cut.run()
    assert failing.latest() is None
    [failure] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert getattr(failure, "event", None) == "sweep_failed"
    assert cut.latest() == _sizes(3, complete=False)
    assert "the figures shown are a floor" in caplog.text


def _wheel(directory: Path) -> None:
    """Write a tiny pure-Python wheel, ``demo-pkg`` 1.0, into `directory`.

    Parameters
    ----------
    directory
        Where the wheel goes.
    """
    files = {
        "demo_pkg/__init__.py": b"X = 1\n",
        "demo_pkg-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.0\n",
        "demo_pkg-1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = [
        f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}"
        for name, data in files.items()
    ]
    record.append("demo_pkg-1.0.dist-info/RECORD,,")
    files["demo_pkg-1.0.dist-info/RECORD"] = ("\n".join(record) + "\n").encode()
    with zipfile.ZipFile(directory / "demo_pkg-1.0-py3-none-any.whl", "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs a real uv on the PATH")
def test_a_real_uv_trims_a_tiny_cache_offline_and_the_environment_still_works(
    paths: CachePaths, tmp_path: Path
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels)
    venv = paths.environments / "demo"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "UV_CACHE_DIR": str(paths.packages),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_CONFIG": "1",
    }
    for argv in (
        [uv, "venv", "-q", "--python", sys.executable, str(venv)],
        [
            uv,
            "pip",
            "install",
            "-q",
            "--python",
            str(venv / "bin" / "python"),
            "--find-links",
            str(wheels),
            "demo-pkg",
        ],
    ):
        subprocess.run(argv, env=env, cwd=tmp_path, check=True, timeout=60)  # nosec B603
    names = caches_module._package_directories(paths.packages)
    assert "demo-pkg" in names
    trim = PackageCacheTrim(
        paths, TrimSettings(uv=uv, max_bytes=1, min_age_seconds=0, max_seconds=60)
    )
    trim.trim(measure(paths))
    assert caches_module._package_directories(paths.packages) == {}
    imported = subprocess.run(  # nosec B603
        [str(venv / "bin" / "python"), "-c", "import demo_pkg; print(demo_pkg.X)"],
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert imported.stdout.strip() == b"1"
