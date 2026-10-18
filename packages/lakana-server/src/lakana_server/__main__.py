"""Entry point for the `lakana-server` console script.

`lakana-server` serves the API with uvicorn; `lakana-server migrate` applies the
metadata database migrations and exits. Both read `LAKANA_*` settings.
"""

from __future__ import annotations

import argparse

import uvicorn

from lakana_core import migrate
from lakana_core.log_setup import configure_logging
from lakana_server.app import create_app
from lakana_server.log_config import uvicorn_log_config
from lakana_server.settings import Settings


def main(argv: list[str] | None = None) -> None:
    """Serve the API, or run a subcommand.

    Parameters
    ----------
    argv
        Command-line arguments without the program name; `sys.argv[1:]` when
        `None`.

    Notes
    -----
    Logging is set up once the settings are read (`lakana_core.log_setup.configure_logging`):
    `LAKANA_LOG__LEVEL` (INFO by default) and above to stderr with time, level and logger
    name, like the worker, so the INFO lines of `lakana_*` loggers reach
    `docker compose logs server`. Uvicorn keeps its own handlers, with formatters that escape
    control characters like the root handler's (`lakana_server.log_config`), and gets the same
    level, so its access log (one line per request) is printed at INFO and DEBUG only, and not
    at all with `LAKANA_LOG__ACCESS=false`.
    """
    parser = argparse.ArgumentParser(prog="lakana-server", description="Run the Lakana API server.")
    parser.add_argument("command", nargs="?", choices=["migrate"], help="migrate: apply migrations")
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log.level)
    if args.command == "migrate":
        migrate.upgrade(settings.database.url)
        return
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log.level.lower(),
        log_config=uvicorn_log_config(),
        access_log=settings.log.access,
    )


if __name__ == "__main__":  # pragma: no cover — exercised only by `python -m lakana_server`
    main()
