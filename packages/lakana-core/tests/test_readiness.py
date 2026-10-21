"""Tests for what the readiness route reads from the database and how it probes a storage."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine, db_now
from lakana_core.models import Base, Environment, Worker
from lakana_core.readiness import (
    MAX_PROBED_ENVIRONMENTS,
    READINESS_CHECKS,
    probe_storage,
    read_database,
)
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.storage.resolver import ResolvedStorage


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provide an engine on a fresh SQLite file with the schema created.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Yields
    ------
    Engine
        The engine; disposed afterwards.
    """
    built = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    Base.metadata.create_all(built)
    yield built
    built.dispose()


def _environment(engine: Engine, name: str, root: str, *, secret: str | None = None) -> None:
    """Insert one live environment.

    Parameters
    ----------
    engine
        The database.
    name
        Its name.
    root
        Its storage root.
    secret
        The name of the secret its storage needs, if any.
    """
    with Session(engine) as session:
        session.add(
            Environment(
                id=uuid.uuid4(),
                name=name,
                storage_root=root,
                storage_options={},
                storage_secret_ref=secret,
                is_protected=False,
                properties={},
            )
        )
        session.commit()


def _worker(engine: Engine, age_seconds: int) -> None:
    """Insert one worker whose heartbeat is this old.

    Parameters
    ----------
    engine
        The database.
    age_seconds
        The age of its heartbeat.
    """
    with Session(engine) as session:
        beat = db_now(session) - timedelta(seconds=age_seconds)
        session.add(
            Worker(id=uuid.uuid4().hex, host="h", version="1", started_at=beat, heartbeat_at=beat)
        )
        session.commit()


def _keys() -> EnvKeyProvider:
    """Build a key provider with no master key.

    Returns
    -------
    EnvKeyProvider
        One that cannot decrypt anything.
    """
    return EnvKeyProvider(None)


def test_the_checks_are_named_in_a_fixed_order() -> None:
    assert READINESS_CHECKS == ("database", "storage", "worker")


def test_a_fresh_heartbeat_counts_and_an_old_one_does_not(engine: Engine) -> None:
    _worker(engine, 61)
    old = read_database(engine, lease_seconds=60, environments=[], key_provider=_keys())
    _worker(engine, 1)
    fresh = read_database(engine, lease_seconds=60, environments=[], key_provider=_keys())

    assert old.worker_alive is False
    assert fresh.worker_alive is True


def test_every_live_environment_is_probed_up_to_the_cap_by_name(
    engine: Engine, tmp_path: Path
) -> None:
    names = [f"env{index:02d}" for index in range(MAX_PROBED_ENVIRONMENTS + 2)]
    for name in reversed(names):
        _environment(engine, name, f"file://{tmp_path}/{name}")

    found = read_database(engine, lease_seconds=60, environments=[], key_provider=_keys())

    assert [target.environment for target in found.storages] == names[:MAX_PROBED_ENVIRONMENTS]
    assert all(target.storage is not None for target in found.storages)


def test_a_deleted_environment_is_not_probed(engine: Engine, tmp_path: Path) -> None:
    _environment(engine, "dev", f"file://{tmp_path}/dev")
    _environment(engine, "old", f"file://{tmp_path}/old")
    with Session(engine) as session:
        row = session.query(Environment).filter_by(name="old").one()
        row.deleted_at = db_now(session)
        session.commit()

    found = read_database(engine, lease_seconds=60, environments=[], key_provider=_keys())

    assert [target.environment for target in found.storages] == ["dev"]


def test_only_the_named_environments_are_probed_and_a_missing_one_fails(
    engine: Engine, tmp_path: Path
) -> None:
    _environment(engine, "dev", f"file://{tmp_path}/dev")
    _environment(engine, "laptop", f"file://{tmp_path}/laptop")

    found = read_database(
        engine, lease_seconds=60, environments=["dev", "prod"], key_provider=_keys()
    )

    assert [(t.environment, t.storage is not None) for t in found.storages] == [
        ("dev", True),
        ("prod", False),
    ]


def test_an_environment_whose_storage_cannot_be_set_up_has_no_storage(
    engine: Engine, tmp_path: Path
) -> None:
    _environment(engine, "dev", f"file://{tmp_path}/dev", secret="s3-keys")

    found = read_database(engine, lease_seconds=60, environments=[], key_provider=_keys())

    assert [(t.environment, t.storage) for t in found.storages] == [("dev", None)]


def test_no_environment_means_nothing_to_probe(engine: Engine) -> None:
    found = read_database(engine, lease_seconds=60, environments=[], key_provider=_keys())

    assert found.storages == ()


def test_an_unreachable_database_raises(tmp_path: Path) -> None:
    missing = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/no/such/dir/lakana.db"))
    try:
        with pytest.raises(OperationalError):
            read_database(missing, lease_seconds=60, environments=[], key_provider=_keys())
    finally:
        missing.dispose()


def test_probing_a_local_storage_succeeds(engine: Engine, tmp_path: Path) -> None:
    _environment(engine, "dev", f"file://{tmp_path}/dev")
    (target,) = read_database(
        engine, lease_seconds=60, environments=[], key_provider=_keys()
    ).storages

    assert target.storage is not None
    probe_storage(target.storage)


def test_a_probe_asks_about_the_root_and_lets_a_storage_error_out() -> None:
    asked: list[str] = []

    def new_input(location: str) -> SimpleNamespace:
        """Record the location and return a file whose `exists` fails.

        Parameters
        ----------
        location
            What the probe asked about.

        Returns
        -------
        SimpleNamespace
            A stand-in input file.
        """
        asked.append(location)

        def exists() -> bool:
            """Fail as an unreachable storage does.

            Raises
            ------
            OSError
                Always.
            """
            raise OSError("AWS Error NETWORK_CONNECTION")

        return SimpleNamespace(exists=exists)

    storage = SimpleNamespace(
        config=SimpleNamespace(root="s3://lakana/dev"), file_io=SimpleNamespace(new_input=new_input)
    )

    with pytest.raises(OSError, match="NETWORK_CONNECTION"):
        probe_storage(cast(ResolvedStorage, storage))
    assert asked == ["s3://lakana/dev"]
