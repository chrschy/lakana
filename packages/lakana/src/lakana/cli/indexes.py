"""`lakana index *`: register and manage package indexes for Python assets."""

from __future__ import annotations

import shlex
from typing import Annotated

import typer

from lakana._models import Secret
from lakana.cli import _output
from lakana.cli._state import CliError, CliState, confirm, state_of
from lakana.errors import LakanaError
from lakana.resources.package_indexes import UPDATABLE

index_app = typer.Typer(
    help="Register and manage package indexes for Python assets.", no_args_is_help=True
)

_LIST_COLUMNS = ["name", "url", "credential_secret"]
_MAX_SHOWN_NAME = 120
"""The longest quoted, escaped secret name a hint shows in a command; a longer one is not cut
(a command that ends inside its quotes is broken) but left out."""


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
    if (
        credential_secret is not None
        and state.output == "table"
        and _still_unbound(state, credential_secret, name)
    ):
        written = _output.inline(shlex.quote(credential_secret), limit=_MAX_SHOWN_NAME + 1)
        index = _output.inline(shlex.quote(name), limit=_MAX_SHOWN_NAME + 1)
        command = (
            f"lakana secret create {written} --env ENV --index {index} --values-stdin"
            if len(written) <= _MAX_SHOWN_NAME and len(index) <= _MAX_SHOWN_NAME
            else f"lakana secret create NAME --env ENV --index {index} --values-stdin (NAME is "
            "the name you gave, too long to show here)"
        )
        typer.echo(
            "Next: someone who may manage secrets creates the secret, bound to this index, in "
            f"each environment whose projects use it: {command} "
            '(a JSON object with "username" and "password").',
            err=True,
        )


def _still_unbound(state: CliState, secret: str, index: str) -> bool:
    """Say whether the secret an index names is still waiting to be bound to it.

    An administrator who registers an index binds the secrets of that name that already exist,
    so nothing is left to do for them. The answer is "unbound" unless a secret of that name was
    found and every one found is bound to this index; when the caller may not list secrets,
    or none exists yet, someone has still to create or bind it.

    Parameters
    ----------
    state
        The CLI state.
    secret
        The credential secret's name.
    index
        The index's name.

    Returns
    -------
    bool
        `True` when a hint to create or bind the secret is still right.
    """
    client = state.client()
    found: list[Secret] = []
    try:
        for environment in client.environments.list():
            try:
                found += [s for s in client.secrets.list(environment.name) if s.name == secret]
            except LakanaError:
                return True
    except LakanaError:
        return True
    return not found or any(s.use != "package_index" or s.use_target != index for s in found)


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
