"""Tests for the programmatic migration runner."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy

from lakana_core import migrate


def _table_names(url: str) -> set[str]:
    """List a database's tables on an engine that is disposed of afterwards.

    Parameters
    ----------
    url
        The database URL.

    Returns
    -------
    set of str
        The table names.
    """
    engine = sqlalchemy.create_engine(url)
    try:
        return set(sqlalchemy.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_creates_the_schema_on_a_fresh_sqlite_file(tmp_path: Path) -> None:
    """`upgrade(url)` applies every revision without needing alembic.ini."""
    url = f"sqlite:///{tmp_path}/lakana.db"

    migrate.upgrade(url)

    tables = _table_names(url)
    assert {"environments", "principals", "alembic_version"} <= tables


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    """Running it twice is a no-op the second time."""
    url = f"sqlite:///{tmp_path}/lakana.db"

    migrate.upgrade(url)
    migrate.upgrade(url)


def test_downgrade_then_upgrade_round_trips(tmp_path: Path) -> None:
    """One step down and back up leaves the schema at head."""
    url = f"sqlite:///{tmp_path}/lakana.db"
    migrate.upgrade(url)

    migrate.downgrade(url, "-1")
    migrate.upgrade(url)

    engine = sqlalchemy.create_engine(url)
    try:
        with engine.connect() as conn:
            versions = conn.execute(
                sqlalchemy.text("SELECT version_num FROM alembic_version")
            ).all()
    finally:
        engine.dispose()
    assert len(versions) == 1


def test_alembic_config_points_at_the_packaged_migrations(tmp_path: Path) -> None:
    """The config uses the installed package's migrations directory and the given URL."""
    url = f"sqlite:///{tmp_path}/lakana.db"

    config = migrate.alembic_config(url)

    script_location = config.get_main_option("script_location")
    assert script_location is not None
    assert Path(script_location).name == "migrations"
    assert config.get_main_option("sqlalchemy.url") == url


def test_alembic_config_survives_a_percent_sign_in_the_url(tmp_path: Path) -> None:
    """A `%` in the URL must not hit ConfigParser interpolation.

    A URL-encoded character (`%40` for `@`, common in a Postgres password)
    used to raise `ValueError: invalid interpolation syntax` from
    `set_main_option`, which goes through `ConfigParser`. The stored option
    must still read back as the exact, unescaped URL.
    """
    url = f"postgresql+psycopg://u:p%40ss@h/{tmp_path.name}"

    config = migrate.alembic_config(url)

    assert config.get_main_option("sqlalchemy.url") == url


def test_upgrade_survives_a_percent_sign_in_the_data_dir_path(tmp_path: Path) -> None:
    """A data directory path containing `%` must not crash a real migration run."""
    weird_dir = tmp_path / "100%-local"
    weird_dir.mkdir()
    url = f"sqlite:///{weird_dir}/lakana.db"

    migrate.upgrade(url)

    tables = _table_names(url)
    assert {"environments", "principals", "alembic_version"} <= tables
