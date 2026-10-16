"""Apply the metadata database migrations without an alembic.ini file.

`alembic.ini` lives outside `src/` and isn't shipped in the wheel, so
installed deployments (`lakana-server migrate`, local mode) build the Alembic
configuration here, from the migrations directory inside `lakana_core`.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

_MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str) -> Config:
    """Build an Alembic config for the packaged migrations and `url`.

    Parameters
    ----------
    url
        SQLAlchemy URL of the metadata database.

    Returns
    -------
    Config
        A config whose `script_location` is `lakana_core/migrations` and whose
        `sqlalchemy.url` is `url`.

    Notes
    -----
    `Config.set_main_option` stores the value in a `ConfigParser`, which
    treats `%` as the start of an interpolation sequence and raises
    `ValueError: invalid interpolation syntax` on any other use of it — a
    URL-encoded character in a password (`%40` for `@`) or a data directory
    path containing `%` would otherwise crash this function outright. `%`
    is escaped as `%%` here for both options before storing them;
    `ConfigParser` un-escapes it back to a literal `%` on read, so
    `get_main_option` returns the exact, unescaped value.
    """
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS).replace("%", "%%"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade(url: str, revision: str = "head") -> None:
    """Upgrade the database at `url` to `revision`.

    Parameters
    ----------
    url
        SQLAlchemy URL of the metadata database.
    revision
        Target revision; `"head"` by default.
    """
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    """Downgrade the database at `url` to `revision`.

    Parameters
    ----------
    url
        SQLAlchemy URL of the metadata database.
    revision
        Target revision, e.g. `"-1"` or a revision id.
    """
    command.downgrade(alembic_config(url), revision)
