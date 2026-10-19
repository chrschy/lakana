"""`lakana search`: find tables, columns, runs and people by a text."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

import typer

from lakana._models import SearchHit
from lakana.cli import _output
from lakana.cli._state import resolve_environment, state_of


class Kind(StrEnum):
    """The values `--kind` accepts."""

    table = "table"
    column = "column"
    run = "run"
    principal = "principal"


def _where(hit: SearchHit) -> str:
    """Say where a hit is, for a reader who has the name.

    Parameters
    ----------
    hit
        The hit.

    Returns
    -------
    str
        The table's kind for a table, the column's type for a column, the job and repository
        for a run, the principal's type for a principal.
    """
    if hit.kind == "table":
        return (hit.table_kind or "").lower()
    if hit.kind == "column":
        return hit.column_type or ""
    if hit.kind == "run":
        return " / ".join(part for part in (hit.repository, hit.job) if part)
    return (hit.principal_type or "").lower()


def search(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(metavar="TEXT", help="What to find; 1 to 200 characters.")],
    kind: Annotated[Kind | None, typer.Option("--kind", help="Only this kind of result.")] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=100, help="Most results to show (1 to 100).")
    ] = 20,
    env: Annotated[
        str | None, typer.Option("--env", help="Environment (default: the profile's).")
    ] = None,
) -> None:
    """Find tables, columns, descriptions, runs and people that match TEXT.

    Matching ignores case and finds TEXT anywhere in a name or description. Exact matches come
    first, then names that start with TEXT, then names that contain it, then descriptions.
    Runs are found by the first four or more characters of their id, their job or their
    repository. You see only what you may read. Use --output json for every field.

    Parameters
    ----------
    ctx
        The Typer context.
    text
        The text to find.
    kind
        Only this kind of result.
    limit
        Most results to show.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    page = state.client().search(
        environment, text, kind=kind.value if kind is not None else None, limit=limit
    )
    if state.output == "json":
        _output.emit(state, page)
        return
    if not page.results:
        _output.emit(
            state,
            f"No match for {_output.inline(repr(text))}. Search looks at table names, column "
            "names, descriptions, run ids, jobs, repositories and (for admins) people; try a "
            "shorter text.",
        )
        return
    _output.emit(
        state,
        page.results,
        columns=["kind", "name", "matched", "where", "snippet"],
        derived={"where": _where},
        droppable=["snippet", "where"],
    )
    if page.has_more:
        _output.emit(state, f"More results follow. Show more with --limit {min(limit * 2, 100)}.")


def register(app: typer.Typer) -> None:
    """Add `search` to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.command("search")(search)
