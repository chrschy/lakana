"""Retention in words, and the `--expire-after` option of the commands that create a table."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

import typer

import lakana
from lakana._models import TableRetention
from lakana.cli import _output
from lakana.cli._state import CliError
from lakana.errors import LakanaError

EXPIRE_AFTER = Annotated[
    int | None,
    typer.Option(
        "--expire-after",
        min=1,
        max=3650,
        help="Let the new table expire this many days after its last write or read.",
    ),
]
"""`--expire-after N` on `table create` and `sql --into ... --mode create`."""

_MOST_DAYS = 3650


def _now() -> datetime:
    """Return the current time (a seam for the tests).

    Returns
    -------
    datetime
        The current UTC time.
    """
    return datetime.now(UTC)


def _moment(when: datetime) -> str:
    """Write a time as people read it, in UTC.

    Parameters
    ----------
    when
        The time.

    Returns
    -------
    str
        For example ``2026-10-31 09:00 UTC``.
    """
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def expiry_text(expires_at: datetime) -> str:
    """Say when a table expires, counted from now.

    Parameters
    ----------
    expires_at
        Its expiry.

    Returns
    -------
    str
        ``expires in N days (date)``, ``expires in less than a day (date)``, or ``is due to
        expire at the next clean-up`` once the time has passed.
    """
    left = expires_at - _now()
    if left.total_seconds() <= 0:
        return "is due to expire at the next clean-up"
    if left.days < 1:
        return f"expires in less than a day ({_moment(expires_at)})"
    unit = "day" if left.days == 1 else "days"
    return f"expires in {left.days} {unit} ({_moment(expires_at)})"


def expires_cell(expires_at: datetime | None) -> str:
    """Say when a table expires, for a column of a listing.

    Parameters
    ----------
    expires_at
        Its expiry, or `None`.

    Returns
    -------
    str
        ``in N days`` and the like; empty without an expiry.
    """
    if expires_at is None:
        return ""
    return expiry_text(expires_at).removeprefix("expires ")


def rule_text(retention: TableRetention) -> str:
    """Describe the rule of a retention in a few words, for a listing.

    Parameters
    ----------
    retention
        The retention.

    Returns
    -------
    str
        ``30 days after last activity (table)``, ``on 2026-11-02 (table)`` and the like.
    """
    rule = f"on {retention.on}" if retention.on else f"{retention.days} days after last activity"
    return f"{rule} ({retention.source})"


def describe(environment: str, retention: TableRetention) -> str:
    """Explain a table's retention in a sentence or two.

    Parameters
    ----------
    environment
        The environment.
    retention
        The retention.

    Returns
    -------
    str
        When it expires, why, and who set the rule.
    """
    name = f"{_output.inline(environment)}/{_output.inline(retention.namespace)}"
    name += f".{_output.inline(retention.name)}"
    if retention.source is None:
        return f"{name} has no retention: it does not expire."
    if retention.never:
        return f"{name} never expires: its retention says so{_set_by(retention)}."
    rule = (
        f"on {retention.on}" if retention.on else f"{retention.days} days after its last activity"
    )
    origin = (
        "set on the table"
        if retention.source == "table"
        else f"the retention of environment {_output.inline(environment)}"
    )
    if retention.kept == "protected":
        return (
            f"{name} does not expire although its retention says {rule}: it is in a protected "
            "environment, whose tables never expire."
        )
    if retention.kept == "project_asset":
        return (
            f"{name} does not expire although its retention says {rule}: a deployed project "
            "builds it, and a project's tables never expire."
        )
    assert retention.expires_at is not None  # nosec B101 -- the server sends one here
    return (
        f"{name} {expiry_text(retention.expires_at)}: {rule} ({origin}"
        f"{_set_by(retention)}). Its last activity was {_moment(retention.last_activity_at)}."
    )


def _set_by(retention: TableRetention) -> str:
    """Name who set a retention, for the end of a sentence.

    Parameters
    ----------
    retention
        The retention.

    Returns
    -------
    str
        `` by NAME`` or nothing.
    """
    return "" if retention.set_by is None else f" by {_output.inline(retention.set_by)}"


def parse_day(text: str) -> date:
    """Read the day of `--on`.

    Parameters
    ----------
    text
        ``YYYY-MM-DD``.

    Returns
    -------
    date
        The day.

    Raises
    ------
    CliError
        If it is not a day (exit 2).
    """
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise CliError(
            f"--on takes a day as YYYY-MM-DD, such as 2026-12-31; got {text!r}."
        ) from None


def parse_within(text: str) -> int:
    """Read the window of `table expiring --within`: days, with or without a ``d``.

    Parameters
    ----------
    text
        ``7d`` or ``7``.

    Returns
    -------
    int
        The days.

    Raises
    ------
    CliError
        If it is not 1 to 3,650 days (exit 2).
    """
    digits = text.removesuffix("d")
    if not digits.isdigit() or not 1 <= int(digits) <= _MOST_DAYS:
        raise CliError(
            f"--within takes 1 to {_MOST_DAYS} days, written like 7d or 7; got {text!r}."
        )
    return int(digits)


def set_expiry(
    client: lakana.Client, environment: str, name: str, days: int, *, created: str
) -> str:
    """Give a table that was just created its retention, for `--expire-after`.

    Parameters
    ----------
    client
        The SDK client.
    environment
        The environment.
    name
        The qualified table name.
    days
        `--expire-after`.
    created
        What was created, for the message when this fails.

    Returns
    -------
    str
        The end of the confirmation, starting with ``; it expires``.

    Raises
    ------
    CliError
        If the server refuses (exit 1): the table exists, and the message says how to try again.
    """
    try:
        client.tables.set_retention(environment, name, days=days)
    except LakanaError as exc:
        raise CliError(
            f"{created}, but could not set its retention: {exc}",
            1,
            hint=(
                f"Set it with `lakana table retention {_output.inline(name)} --days {days} "
                f"--env {_output.inline(environment)}`."
            ),
            kind=type(exc).__name__,
        ) from exc
    return f"; it expires {days} days after its last activity"
