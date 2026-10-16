"""`lakana table *`: manage tables and load data into them."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Annotated

import typer

import lakana
from lakana._models import Column, Lineage, Table
from lakana.cli import _files, _output
from lakana.cli._break_glass import BREAK_GLASS, REASON, break_glass_reason
from lakana.cli._state import CliError, confirm, resolve_environment, state_of

table_app = typer.Typer(help="Manage tables and load data.", no_args_is_help=True)

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]
_FORMAT = Annotated[
    str | None,
    typer.Option("--format", help="parquet, csv or arrow (default: from the extension)."),
]
_SOURCE = Annotated[
    list[str] | None,
    typer.Option("--source", help="A table the data came from (repeatable); recorded as lineage."),
]


def parse_column_spec(spec: str) -> dict[str, object]:
    """Parse `name:type[?]` into the API's JSON column form.

    Parameters
    ----------
    spec
        E.g. `"id:long"` or `"amount:decimal(10,2)?"` (`?` = nullable).

    Returns
    -------
    dict
        `{"name", "type", "nullable"}`.

    Raises
    ------
    CliError
        If the name or type is missing (exit 2).
    """
    name, sep, type_part = spec.partition(":")
    nullable = type_part.endswith("?")
    type_name = type_part[:-1] if nullable else type_part
    if not sep or not name or not type_name:
        raise CliError(f"--column expects name:type or name:type?, got {spec!r}")
    return {"name": name, "type": type_name, "nullable": nullable}


def _load_schema_file(path: Path) -> list[dict[str, object]]:
    """Read a JSON column list from a file.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    list of dict
        The column list.

    Raises
    ------
    CliError
        If the file is missing, not JSON, or not a list (exit 8).
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"Could not read schema file {path}: {exc}", 8, kind="FileError") from exc
    if not isinstance(data, list):
        raise CliError(f"{path} must contain a JSON list of columns", 8, kind="FileError")
    return data


@table_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    schema: Annotated[
        Path | None, typer.Option("--schema", help="JSON file with the column list.")
    ] = None,
    column: Annotated[
        list[str] | None,
        typer.Option(
            "--column",
            help=(
                "name:type, ? suffix = nullable (repeatable). Quote a ? spec, "
                "e.g. 'region:string?', so the shell doesn't glob it."
            ),
        ),
    ] = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Create managed table NAME (namespace.table).

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    env
        The environment.
    schema
        A JSON column-list file.
    column
        Column specs.
    break_glass
        `--break-glass`.
    reason
        `--reason`.

    Raises
    ------
    CliError
        Unless exactly one of `--schema`/`--column` is given (exit 2).
    """
    state = state_of(ctx)
    if (schema is None) == (not column):
        raise CliError("Pass exactly one of --schema FILE or --column name:type")
    columns = (
        _load_schema_file(schema)
        if schema is not None
        else [parse_column_spec(c) for c in column or []]
    )
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    table = state.client().tables.create(
        environment, name, schema=columns, break_glass_reason=glass
    )
    _output.done(
        state, f"Created table {_output.inline(environment)}/{_output.inline(name)}", table
    )


@table_app.command("create-external")
def create_external(
    ctx: typer.Context,
    name: str,
    location: Annotated[
        str, typer.Option("--location", help="Pattern under raw/, e.g. raw/orders/*.csv")
    ],
    file_format: Annotated[str, typer.Option("--format", help="csv, json or parquet")],
    env: _ENV = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Register external table NAME over files under raw/.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    location
        The location pattern.
    file_format
        The files' format.
    env
        The environment.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    table = state.client().tables.create_external(
        environment, name, location, file_format, break_glass_reason=glass
    )
    _output.done(
        state, f"Created external table {_output.inline(environment)}/{_output.inline(name)}", table
    )


def _state_cell(table: Table) -> str:
    """Say whether a table is published, for the `state` column.

    Parameters
    ----------
    table
        A table, view, external table or artifact as the server described it.

    Returns
    -------
    str
        ``published``, ``not published`` or ``kept`` for a managed table; empty for a view, an
        external table and an artifact, which have no first build to fail.
    """
    if table.kind != "TABLE":
        return ""
    return {"not_published": "not published", "kept": "kept"}.get(table.state, "published")


_GET_COLUMNS = ["name", "namespace", "kind", "state", "uuid"]
"""The rows `table get` shows for every kind; `state` is empty where a kind has none."""
_DERIVED = {"state": _state_cell}


@table_app.command("list")
def list_tables(ctx: typer.Context, namespace: str, env: _ENV = None) -> None:
    """List the tables in NAMESPACE.

    Parameters
    ----------
    ctx
        The Typer context.
    namespace
        The dotted namespace.
    env
        The environment.
    """
    state = state_of(ctx)
    tables = state.client().tables.list(resolve_environment(state, env), namespace)
    _output.emit(
        state,
        tables,
        columns=["name", "namespace", "kind", "state", "uuid"],
        droppable=["kind", "uuid"],
        derived={"state": _state_cell},
    )


@table_app.command("get")
def get(ctx: typer.Context, name: str, env: _ENV = None) -> None:
    """Show table or view NAME, its columns, and a view's SQL and references.

    A table whose first build did not finish, or failed a check, is shown as `not published`;
    one that keeps the data that failed a check (`on_failure: keep`), as `kept`.
    An artifact is shown with its name and a pointer to `lakana artifact versions list`.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    env
        The environment.
    """
    state = state_of(ctx)
    table = state.client().tables.get(resolve_environment(state, env), name)
    if state.output == "json":
        _output.emit(state, table)
        return
    if table.kind == "VIEW":
        _output.emit(state, table, columns=_GET_COLUMNS, derived=_DERIVED)
        if table.hidden_references:
            _output.emit(
                state,
                f"SQL hidden: {table.hidden_references} referenced table(s) you can't read",
            )
        else:
            _output.emit(state, table.sql or "")
        _output.emit(state, table.references)
        _output.emit(state, table.columns)
        return
    if table.kind == "ARTIFACT":
        _output.emit(state, table, columns=_GET_COLUMNS, derived=_DERIVED)
        _output.emit(
            state,
            f"{_output.inline(name)} is an artifact. See its versions with: "
            f"lakana artifact versions list {shlex.quote(_output.inline(name))}",
        )
        return
    _output.emit(state, table, columns=[*_GET_COLUMNS, "metadata_location"], derived=_DERIVED)
    if table.state_message:
        _output.emit(state, table.state_message)
    _output.emit(state, table.columns)


@table_app.command("delete")
def delete(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    yes: _YES = False,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Drop table NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    env
        The environment.
    yes
        Skip the confirmation prompt.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    confirm(state, f"Delete table {environment}/{name}?", yes)
    state.client().tables.delete(environment, name, break_glass_reason=glass)
    _output.done(state, f"Deleted table {_output.inline(environment)}/{_output.inline(name)}")


def _table_columns(client: lakana.Client, environment: str, name: str) -> list[Column]:
    """Fetch a table's columns, to conform a file to them.

    Parameters
    ----------
    client
        The SDK client.
    environment
        The environment.
    name
        The qualified table name.

    Returns
    -------
    list of Column
        The table's columns.

    Raises
    ------
    CliError
        If the server reports no columns (an older server; exit 11).
    """
    table = client.tables.get(environment, name)
    if not table.columns:
        # A table always has at least one column; an empty list means the server
        # is too old to report schemas, so there is nothing to conform to.
        raise CliError(
            f"The server didn't report {environment}/{name}'s columns; upgrade the Lakana "
            "server to a version that reports a table's columns to use this command.",
            11,
            kind="IncompatibleServer",
        )
    return table.columns


@table_app.command("append")
def append(
    ctx: typer.Context,
    name: str,
    file: Path,
    env: _ENV = None,
    file_format: _FORMAT = None,
    source: _SOURCE = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Append FILE (Parquet, CSV or Arrow) to table NAME.

    The table's schema is fetched first. A CSV is parsed straight into the
    table's column types; Parquet and Arrow are cast to them. Nullability is
    checked too, and a column that doesn't fit fails here, before anything
    is sent.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    file
        The data file.
    env
        The environment.
    file_format
        The file's format, if the extension doesn't say.
    source
        Tables the data came from, recorded as lineage.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    fmt = _files.infer_format(file, file_format)
    client = state.client()
    columns = _table_columns(client, environment, name)
    data = _files.read_table(file, fmt, columns)
    result = client.tables.append(
        environment,
        name,
        _files.conform(data, columns),
        sources=source or None,
        break_glass_reason=glass,
    )
    _output.done(
        state,
        f"Appended {result.rows} rows to {_output.inline(environment)}/{_output.inline(name)}",
        result,
    )


@table_app.command("replace")
def replace(
    ctx: typer.Context,
    name: str,
    file: Path,
    env: _ENV = None,
    file_format: _FORMAT = None,
    evolve_schema: Annotated[
        bool, typer.Option("--evolve-schema", help="Let the table adopt the file's schema.")
    ] = False,
    source: _SOURCE = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Replace every row of table NAME with FILE (Parquet, CSV or Arrow).

    Without --evolve-schema the file is conformed to the table's columns
    first, exactly as `table append` does. With it, the file is sent as it
    is and the table takes its schema.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    file
        The data file.
    env
        The environment.
    file_format
        The file's format, if the extension doesn't say.
    evolve_schema
        Let the table adopt the file's schema.
    source
        Tables the data came from, recorded as lineage.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    fmt = _files.infer_format(file, file_format)
    client = state.client()
    if evolve_schema:
        data = _files.read_table(file, fmt)
    else:
        columns = _table_columns(client, environment, name)
        data = _files.conform(_files.read_table(file, fmt, columns), columns)
    result = client.tables.replace(
        environment,
        name,
        data,
        evolve_schema=evolve_schema,
        sources=source or None,
        break_glass_reason=glass,
    )
    _output.done(
        state,
        f"Replaced {_output.inline(environment)}/{_output.inline(name)} with {result.rows} rows",
        result,
    )


_FAILED_CHECK_TAG_DAYS = 7
"""Days after which maintenance removes the tag of a failed check (the server's rule)."""


def _maintained(environment: str, name: str, report: dict[str, object]) -> str:
    """Describe a finished maintenance run in one line.

    Parameters
    ----------
    environment
        The environment.
    name
        The table.
    report
        The run's `maintenance["report"]`.

    Returns
    -------
    str
        What was (or, for a dry run, would be) expired and deleted, the tags of
        failed checks removed (when there were any), then the report's note, if any.
    """
    expired_ids = report.get("snapshots_expired")
    expired = len(expired_ids) if isinstance(expired_ids, list) else 0
    files, size = report.get("files_deleted", 0), report.get("bytes_deleted", 0)
    if report.get("dry_run"):
        line = (
            f"Dry run for {_output.inline(environment)}/{_output.inline(name)}: "
            f"would expire {expired} snapshots, would delete {files} files ({size} bytes)"
        )
    else:
        line = (
            f"Maintained {_output.inline(environment)}/{_output.inline(name)}: "
            f"expired {expired} snapshots, deleted {files} files ({size} bytes)"
        )
    removed = report.get("tags_removed")
    tags = len(removed) if isinstance(removed, list) else 0
    if tags:
        verb = "would remove" if report.get("dry_run") else "removed"
        what = "1 tag of a failed check" if tags == 1 else f"{tags} tags of failed checks"
        line += f", {verb} {what} older than {_FAILED_CHECK_TAG_DAYS} days"
    note = report.get("note")
    return f"{line}. {_output.inline(str(note), limit=1000)}" if note else line


@table_app.command("maintain")
def maintain(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    keep_days: Annotated[
        int, typer.Option("--keep-days", min=0, help="Keep snapshots younger than this.")
    ] = 7,
    keep_last: Annotated[
        int, typer.Option("--keep-last", min=1, help="Always keep this many recent snapshots.")
    ] = 5,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would happen; change nothing.")
    ] = False,
    older_than_hours: Annotated[
        int,
        typer.Option(
            "--older-than-hours",
            min=1,
            max=87600,
            help="Orphaned files younger than this many hours are never deleted (1 to 87600).",
        ),
    ] = 72,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Seconds to wait before giving up.")
    ] = None,
) -> None:
    """Expire table NAME's old snapshots, delete its orphaned files, and remove failed-check tags.

    A snapshot kept under the tag of a failed check is removed with that tag once it is older
    than 7 days. A snapshot that a run still in progress needs to set its table back is kept.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    env
        The environment.
    keep_days
        Snapshots younger than this many days are kept.
    keep_last
        The most recent this many snapshots are kept.
    dry_run
        Report only.
    older_than_hours
        Orphans younger than this many hours are never deleted (1 to 87600).
    timeout
        Seconds to wait for the run.

    Raises
    ------
    CliError
        With exit code 1 if the report's `conflict` is set: the table
        changed during maintenance and nothing was applied; the caller
        should retry. The note's wording is never compared.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    run = state.client().tables.maintain(
        environment,
        name,
        keep_days=keep_days,
        keep_last=keep_last,
        dry_run=dry_run,
        older_than_hours=older_than_hours,
    )
    status = run.wait(timeout=timeout)
    report = (status.maintenance or {}).get("report", {})
    if report.get("conflict") is True:
        raise CliError(
            f"Maintenance of {environment}/{name} not applied: "
            "table changed during maintenance; retry",
            1,
            kind="MaintenanceNotApplied",
        )
    _output.done(state, _maintained(environment, name, report), status)


def lineage_tree(lineage: Lineage) -> list[str]:
    """Render a lineage graph as tree lines, the root first.

    A node is expanded once, at its first occurrence; later occurrences
    (a self-edge, a diamond, a shared ancestor) print as ``(see above)``, so
    the output is linear in the edges however the graph converges.

    Parameters
    ----------
    lineage
        The graph.

    Returns
    -------
    list of str
        One line per node, plus a final line counting hidden nodes.
    """
    nodes = {node.id: node for node in lineage.nodes}
    children: dict[str, list[str]] = {}
    for edge in lineage.edges:
        parent, child = (
            (edge.target_id, edge.source_id)
            if lineage.direction == "upstream"
            else (edge.source_id, edge.target_id)
        )
        children.setdefault(parent, []).append(child)

    def label(node_id: str) -> str:
        node = nodes[node_id]
        text = node.name
        if node.kind not in (None, "TABLE"):
            text += f" [{node.kind.lower()}]"
        if node.dropped:
            text += " (dropped)"
        return text

    lines = [label(lineage.root_id)]
    expanded = {lineage.root_id}

    def walk(node_id: str, prefix: str) -> None:
        kids = sorted(children.get(node_id, []), key=lambda kid: nodes[kid].name)
        for index, kid in enumerate(kids):
            last = index == len(kids) - 1
            text = label(kid)
            if kid in expanded:
                text += " (see above)"
            lines.append(f"{prefix}{'└── ' if last else '├── '}{text}")
            if kid not in expanded:
                expanded.add(kid)
                walk(kid, prefix + ("    " if last else "│   "))

    walk(lineage.root_id, "")
    if lineage.hidden_nodes == 1:
        lines.append("(1 more node hidden: you can't read it)")
    elif lineage.hidden_nodes:
        lines.append(f"({lineage.hidden_nodes} more nodes hidden: you can't read them)")
    if lineage.truncated:
        lines.append("… truncated at 500 nodes")
    return lines


@table_app.command("lineage")
def lineage(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    downstream: Annotated[
        bool, typer.Option("--downstream", help="Show what NAME feeds, not what feeds it.")
    ] = False,
    depth: Annotated[int, typer.Option("--depth", min=1, max=10, help="Hops to walk.")] = 3,
) -> None:
    """Show table NAME's lineage as a tree (or JSON with --output json).

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    env
        The environment.
    downstream
        Walk downstream instead of upstream.
    depth
        Hops to walk.
    """
    state = state_of(ctx)
    graph = state.client().tables.lineage(
        resolve_environment(state, env),
        name,
        direction="downstream" if downstream else "upstream",
        depth=depth,
    )
    if state.output == "json":
        _output.emit(state, graph)
        return
    typer.echo("\n".join(lineage_tree(graph)))


def register(app: typer.Typer) -> None:
    """Add the `table` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(table_app, name="table")
