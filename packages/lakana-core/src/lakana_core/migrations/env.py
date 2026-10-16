"""Alembic environment configuration for lakana-core.

The database URL is not hard-coded in ``alembic.ini``. `lakana_core.migrate`
(used by installed deployments, where ``alembic.ini`` isn't shipped) sets
``sqlalchemy.url`` on the `Config` it builds before running migrations, and
this module leaves that URL alone. When no URL is preset — ``alembic.ini``
plus the CLI, as in tests and local development — it falls back to
:class:`lakana_core.config.Settings`, which resolves it from the
``LAKANA_DATABASE__URL`` environment variable. Batch mode is enabled for both
migration modes because SQLite cannot ``ALTER`` a column in place, and
Lakana's migrations must run against both SQLite and Postgres.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from lakana_core.config import Settings
from lakana_core.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # Keep loggers that already exist: a process that migrates in-process
    # (and the test suite) must not lose its module loggers.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# add your model's MetaData object here for 'autogenerate' support
target_metadata = Base.metadata

# `lakana_core.migrate` sets the URL explicitly; alembic.ini (tests, dev) doesn't,
# and then the URL comes from `LAKANA_DATABASE__URL` via Settings. `%` is escaped
# as `%%` before storing it, same as `lakana_core.migrate.alembic_config`: the
# `Config` is backed by a `ConfigParser`, which raises `ValueError: invalid
# interpolation syntax` on a bare `%` -- a URL-encoded password character
# (`%40` for `@`) would otherwise crash this fallback outright.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", Settings().database.url.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, though an
    Engine is acceptable here as well. By skipping the Engine creation we
    don't even need a DBAPI to be available. Calls to ``context.execute()``
    here emit the given string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a connection
    with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
