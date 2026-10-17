"""`lakana index *`: register and manage package indexes for Python assets."""

from __future__ import annotations

import shlex
from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import CliError, confirm, state_of
from lakana.resources.package_indexes import UPDATABLE

index_app = typer.Typer(
    help="Register and manage package indexes for Python assets.", no_args_is_help=True
)

_LIST_COLUMNS = ["name", "url", "credential_secret"]
_WHOLE = 100_000
"""Far above any name, so that a name in a command is escaped but never cut."""


@index_app.command("add")
def add(
    ctx: typer.Context,
    name: str,
    url: Annotated[
        str,
        typer.Option(
            "--url", help="https://host/path of the index, without a user name or password."
        ),
    ],
    credential_secret: Annotated[
        str | None,
        typer.Option(
            "--credential-secret",
            help="Name of the secret that holds username and password, per environment.",
        ),
    ] = None,
) -> None:
    """Register package index NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The index name.
    url
        Its URL.
    credential_secret
        Name of the secret that holds the credential.
    """
    state = state_of(ctx)
    index = state.client().package_indexes.create(name, url, credential_secret=credential_secret)
    _output.done(state, f"Registered package index {_output.inline(name)}", index)
    if credential_secret is not None and state.output == "table":
        secret = _output.inline(shlex.quote(credential_secret), limit=_WHOLE)
        index_name = _output.inline(shlex.quote(name), limit=_WHOLE)
        typer.echo(
            f"Next: in each environment where the secret {secret} does not exist yet, someone who "
            "may manage secrets creates it, bound to this index (the values are a JSON object "
            'with "username" and "password"):\n'
            f"  lakana secret create {secret} --env ENV --index {index_name} --values-stdin\n"
            "Where it exists but is not bound to this index, they bind it:\n"
            f"  lakana secret bind {secret} --env ENV --index {index_name}",
            err=True,
        )


@index_app.command("list")
def list_indexes(ctx: typer.Context) -> None:
    """List package indexes.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(
        state,
        state.client().package_indexes.list(),
        columns=_LIST_COLUMNS,
        droppable=["credential_secret"],
        whole=["url"],
    )


@index_app.command("show")
def show(ctx: typer.Context, name: str) -> None:
    """Show package index NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The index name.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().package_indexes.get(name))


@index_app.command("update")
def update(
    ctx: typer.Context,
    name: str,
    url: Annotated[str | None, typer.Option("--url")] = None,
    credential_secret: Annotated[str | None, typer.Option("--credential-secret")] = None,
    clear: Annotated[
        list[str] | None, typer.Option("--clear", help="Clear a field (repeatable).")
    ] = None,
) -> None:
    """Change package index NAME; only the options given change.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The index name.
    url
        New URL.
    credential_secret
        New secret name.
    clear
        Fields to clear.

    Raises
    ------
    CliError
        If nothing would change, `--clear` names an unknown field, or a field is
        both set and cleared (exit 2).
    """
    state = state_of(ctx)
    changes: dict[str, str | None] = {
        key: value
        for key, value in {"url": url, "credential_secret": credential_secret}.items()
        if value is not None
    }
    for field in clear or []:
        if field not in UPDATABLE - {"url"}:
            raise CliError(
                f"--clear accepts {', '.join(sorted(UPDATABLE - {'url'}))}; got {field!r}"
            )
        if field in changes:
            raise CliError(f"Can't both set and clear {field}; pass only one.")
        changes[field] = None
    if not changes:
        raise CliError("Nothing to change; pass at least one option.")
    index = state.client().package_indexes.update(name, **changes)
    _output.done(state, f"Updated package index {_output.inline(name)}", index)


@index_app.command("remove")
def remove(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete package index NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The index name.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(
        state,
        f"Delete package index {_output.inline(name)}? Projects whose lock names it stop "
        "running until it is registered again.",
        yes,
    )
    state.client().package_indexes.delete(name)
    _output.done(state, f"Deleted package index {_output.inline(name)}")


def register(app: typer.Typer) -> None:
    """Add the `index` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(index_app, name="index")
