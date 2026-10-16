"""`lakana audit list`: read the audit log."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import CliError, state_of

audit_app = typer.Typer(help="Read the audit log.", no_args_is_help=True)

_EVENT_COLUMNS = [
    "occurred_at",
    "event_type",
    "principal_id",
    "securable_environment",
    "securable_namespace",
    "securable_table",
]
_EVENT_DROPPABLE = [
    "securable_table",
    "securable_namespace",
    "securable_environment",
    "principal_id",
]
"""What the audit listing leaves out first: the time and the kind of event always stay."""


def parse_timestamp(value: str | None, flag: str) -> datetime | None:
    """Parse an ISO 8601 timestamp, treating one without an offset as UTC.

    Parameters
    ----------
    value
        The raw option value.
    flag
        The option's name, for the error message.

    Returns
    -------
    datetime or None
        An aware datetime, or `None`.

    Raises
    ------
    CliError
        If the value isn't ISO 8601 (exit 2).
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CliError(f"{flag} expects an ISO 8601 timestamp, got {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@audit_app.command("list")
def list_events(
    ctx: typer.Context,
    event_type: Annotated[
        str | None, typer.Option("--event-type", help="e.g. GRANT_CREATED")
    ] = None,
    env: Annotated[str | None, typer.Option("--env", help="Only this environment.")] = None,
    principal_id: Annotated[
        str | None, typer.Option("--principal-id", help="Only this principal's events.")
    ] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="ISO 8601; UTC if no offset.")
    ] = None,
    until: Annotated[
        str | None, typer.Option("--until", help="ISO 8601; UTC if no offset.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500, help="Events per page.")] = 100,
    cursor: Annotated[
        str | None, typer.Option("--cursor", help="Continue from a previous page.")
    ] = None,
    all_pages: Annotated[bool, typer.Option("--all", help="Follow every page.")] = False,
) -> None:
    """List audit events, newest first.

    Parameters
    ----------
    ctx
        The Typer context.
    event_type
        Only events of this type.
    env
        Only events in this environment.
    principal_id
        Only events by this principal.
    since
        Only events at or after this time.
    until
        Only events at or before this time.
    limit
        Events per page.
    cursor
        A previous page's `next_cursor`.
    all_pages
        Follow `next_cursor` to the end.

    Raises
    ------
    CliError
        For `--cursor` with `--all`, or a bad timestamp (exit 2).
    """
    state = state_of(ctx)
    if all_pages and cursor:
        raise CliError("--cursor and --all can't be combined")
    filters = {
        "event_type": event_type,
        "environment": env,
        "principal_id": principal_id,
        "since": parse_timestamp(since, "--since"),
        "until": parse_timestamp(until, "--until"),
    }
    events = state.client().audit_events
    if all_pages:
        _output.emit(
            state,
            list(events.iter(**filters, page_size=limit)),
            columns=_EVENT_COLUMNS,
            droppable=_EVENT_DROPPABLE,
        )
        return
    page = events.list(**filters, limit=limit, cursor=cursor)
    if state.output == "json":
        _output.print_json(page)
        return
    _output.emit(state, page.items, columns=_EVENT_COLUMNS, droppable=_EVENT_DROPPABLE)
    if page.next_cursor:
        sys.stdout.write(f"More: --cursor {page.next_cursor}\n")


def register(app: typer.Typer) -> None:
    """Add the `audit` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(audit_app, name="audit")
