"""The `lakana-worker` command: the worker, or the health check a container runs next to it.

Kept apart from `lakana_worker.__main__` so that the health check, which a container runs every
few seconds, does not import the query engine and the rest of the worker.
"""

from __future__ import annotations

import argparse

from lakana_worker.healthcheck import healthcheck


def cli(argv: list[str] | None = None) -> None:
    """Run the worker, or the health check.

    Parameters
    ----------
    argv
        Command-line arguments without the program name; `sys.argv[1:]` when `None`.

    Raises
    ------
    SystemExit
        After `healthcheck`, with its exit code (0 healthy, 1 not); with 2 for an unknown
        command.
    """
    parser = argparse.ArgumentParser(prog="lakana-worker", description="Run a Lakana worker.")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["healthcheck"],
        help=(
            "healthcheck: exit 0 if the worker of this host wrote a heartbeat within "
            "LAKANA_RUN__LEASE_SECONDS, 1 if not or if the database does not answer"
        ),
    )
    args = parser.parse_args(argv)
    if args.command == "healthcheck":
        raise SystemExit(healthcheck())
    from lakana_worker.__main__ import main  # the worker itself: imported only to run it

    main()
