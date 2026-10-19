"""What the list commands share: the `--since` option and the line that names the next page."""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from datetime import datetime

import typer

from lakana.cli import _output
from lakana.cli._state import CliError, CliState


def parse_since(value: str | None) -> datetime | None:
    """Read the value of `--since` as a time with a time zone.

    Parameters
    ----------
    value
        An ISO 8601 date or time, such as ``2026-10-18``, ``2026-10-18T09:30`` or
        ``2026-10-18T09:30:00Z``; `None` when the option was not given.

    Returns
    -------
    datetime or None
        The time. One written without a time zone is read in the time zone of this computer.

    Raises
    ------
    CliError
        If the value is not an ISO 8601 date or time.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CliError(
            f"--since {_output.inline(value)!r} is not a date or time. Write it like "
            "2026-10-18 or 2026-10-18T09:30:00Z."
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def more_results(
    state: CliState,
    command: Sequence[str],
    options: Mapping[str, str | int | bool | Sequence[str] | None],
    cursor: str | None,
) -> None:
    """Tell the user how to fetch the next page of a list, if there is one.

    The line goes to stdout in table mode and to stderr with ``--output json``, so the JSON on
    stdout stays whole.

    Parameters
    ----------
    state
        The CLI state (for the output mode).
    command
        The words of the command, such as ``["lakana", "runs", "list"]``.
    options
        The options of this call by flag, such as ``{"--status": ["failed"]}``; an option that is
        `None`, empty or `False` is left out and `True` is a flag without a value.
    cursor
        The `next_cursor` of the page just shown, or `None` on the last page.
    """
    if cursor is None:
        return
    words = list(command)
    for flag, value in {**options, "--cursor": cursor}.items():
        if value is None or value is False or value == []:
            continue
        words.append(flag)
        if value is not True:
            shown = ",".join(value) if isinstance(value, list | tuple) else str(value)
            words.append(shlex.quote(_output.inline(shown)))
    typer.echo("More results: " + " ".join(words), err=state.output == "json")
