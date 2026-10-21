"""Tests for `lakana-worker healthcheck`, the check a container runs against its own worker."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from lakana_core.config import DatabaseConfig, Settings
from lakana_core.db import create_engine, db_now
from lakana_core.models import Base, Worker
from lakana_worker import command
from lakana_worker.healthcheck import check_health, run_healthcheck


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Provide a migrated SQLite file and point `LAKANA_DATABASE__URL` at it.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Sets the variable for this test.

    Yields
    ------
    str
        The database URL.
    """
    url = f"sqlite:///{tmp_path}/lakana.db"
    engine = create_engine(DatabaseConfig(url=url))
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("LAKANA_DATABASE__URL", url)
    yield url


def _worker(url: str, host: str, age_seconds: int) -> None:
    """Insert a worker of this host whose heartbeat is this old.

    Parameters
    ----------
    url
        The database URL.
    host
        The worker's host.
    age_seconds
        The age of its heartbeat.
    """
    engine = create_engine(DatabaseConfig(url=url))
    try:
        with Session(engine) as session:
            beat = db_now(session) - timedelta(seconds=age_seconds)
            session.add(
                Worker(id=f"{host}:1:x", host=host, version="1", started_at=beat, heartbeat_at=beat)
            )
            session.commit()
    finally:
        engine.dispose()


def test_a_fresh_heartbeat_of_this_host_is_healthy(database: str) -> None:
    _worker(database, "box", 1)

    assert check_health(Settings(), "box") is None


def test_a_stale_heartbeat_says_what_it_means_and_what_to_read(database: str) -> None:
    _worker(database, "box", 61)
    _worker(database, "other", 1)

    message = check_health(Settings(), "box")

    assert message is not None
    assert "60 s (LAKANA_RUN__LEASE_SECONDS)" in message
    assert "cannot reach the database" in message and "log" in message


def test_an_unreachable_database_names_the_setting(tmp_path: Path) -> None:
    settings = Settings(database=DatabaseConfig(url=f"sqlite:///{tmp_path}/no/such/lakana.db"))

    message = check_health(settings, "box")

    assert message is not None
    assert "LAKANA_DATABASE__URL" in message
    assert str(tmp_path) not in message


def test_a_check_that_hangs_fails_within_the_time_limit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    release = threading.Event()

    def hang() -> str | None:
        """Wait until the test ends.

        Returns
        -------
        str or None
            Healthy, once released.
        """
        release.wait(10)
        return None

    try:
        assert run_healthcheck(hang, timeout=0.05) == 1
    finally:
        release.set()
    assert "within 0.05 s" in capsys.readouterr().err


def test_a_check_that_raises_fails_with_the_error_class(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def check() -> str | None:
        """Fail unexpectedly.

        Raises
        ------
        ValueError
            Always.
        """
        raise ValueError("secret text")

    assert run_healthcheck(check, timeout=1) == 1
    err = capsys.readouterr().err
    assert "ValueError" in err and "secret text" not in err


def test_the_command_exits_0_when_healthy_and_1_when_not(
    database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("lakana_worker.healthcheck.socket.gethostname", lambda: "box")

    with pytest.raises(SystemExit) as unhealthy:
        command.cli(["healthcheck"])
    _worker(database, "box", 1)
    with pytest.raises(SystemExit) as healthy:
        command.cli(["healthcheck"])

    assert (unhealthy.value.code, healthy.value.code) == (1, 0)
    out = capsys.readouterr()
    assert "No worker on this host" in out.err and "healthy" in out.out


def test_the_command_refuses_settings_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LAKANA_DATABASE__URL", raising=False)

    with pytest.raises(SystemExit) as refused:
        command.cli(["healthcheck"])

    assert refused.value.code == 1
    assert "database" in capsys.readouterr().err


def test_without_a_command_the_worker_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[bool] = []
    monkeypatch.setattr("lakana_worker.__main__.main", lambda: ran.append(True))

    command.cli([])

    assert ran == [True]


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit) as refused:
        command.cli(["heathcheck"])

    assert refused.value.code == 2


def test_the_health_check_does_not_import_the_query_engine() -> None:
    """It runs every few seconds next to the worker; the engine would double its memory."""
    import subprocess
    import sys

    code = "import sys, lakana_worker.command; print('duckdb' in sys.modules)"
    found = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )

    assert found.stdout.strip() == "False"
