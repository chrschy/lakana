"""`lakana repo *`: register and manage git repositories."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import CliError, confirm, state_of
from lakana.resources.repositories import UPDATABLE

repo_app = typer.Typer(help="Register and manage git repositories.", no_args_is_help=True)

_LIST_COLUMNS = ["name", "kind", "url", "default_ref", "credential_secret"]


def _read_known_hosts(path: Path | None) -> str | None:
    """Read a known_hosts file given on the command line.

    Parameters
    ----------
    path
        `--known-hosts-file`, or `None`.

    Returns
    -------
    str or None
        Its text.

    Raises
    ------
    CliError
        If it cannot be read (exit 2).
    """
    if path is None:
        return None
    try:
        return path.read_text()
    except OSError as exc:
        raise CliError(f"Can't read {path}: {exc}") from exc


@repo_app.command("add")
def add(
    ctx: typer.Context,
    name: str,
    kind: Annotated[str, typer.Option("--kind", help="local, https or ssh.")],
    url: Annotated[str, typer.Option("--url", help="URL, or a path for --kind local.")],
    credential_secret: Annotated[
        str | None,
        typer.Option("--credential-secret", help="Secret name, resolved per environment."),
    ] = None,
    credential_username: Annotated[
        str | None, typer.Option("--credential-username", help="HTTPS username (default oauth2).")
    ] = None,
    known_hosts_file: Annotated[
        Path | None, typer.Option("--known-hosts-file", help="known_hosts lines for --kind ssh.")
    ] = None,
    default_ref: Annotated[
        str | None, typer.Option("--default-ref", help="Ref deployed by default.")
    ] = None,
) -> None:
    """Register repository NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The repository name.
    kind
        Its kind.
    url
        Its URL or path.
    credential_secret
        Secret name.
    credential_username
        HTTPS username.
    known_hosts_file
        A known_hosts file.
    default_ref
        Default ref.
    """
    state = state_of(ctx)
    if kind == "local":
        url = str(Path(url).expanduser().resolve())
    repo = state.client().repositories.create(
        name,
        kind,
        url,
        credential_secret=credential_secret,
        credential_username=credential_username,
        ssh_known_hosts=_read_known_hosts(known_hosts_file),
        default_ref=default_ref,
    )
    _output.done(state, f"Registered repository {_output.inline(name)}", repo)


@repo_app.command("list")
def list_repos(ctx: typer.Context) -> None:
    """List repositories.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(
        state,
        state.client().repositories.list(),
        columns=_LIST_COLUMNS,
        droppable=["credential_secret", "default_ref", "kind"],
    )


@repo_app.command("show")
def show(ctx: typer.Context, name: str) -> None:
    """Show repository NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The repository name.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().repositories.get(name))


@repo_app.command("update")
def update(
    ctx: typer.Context,
    name: str,
    url: Annotated[str | None, typer.Option("--url")] = None,
    credential_secret: Annotated[str | None, typer.Option("--credential-secret")] = None,
    credential_username: Annotated[str | None, typer.Option("--credential-username")] = None,
    known_hosts_file: Annotated[Path | None, typer.Option("--known-hosts-file")] = None,
    default_ref: Annotated[str | None, typer.Option("--default-ref")] = None,
    clear: Annotated[
        list[str] | None, typer.Option("--clear", help="Clear a field (repeatable).")
    ] = None,
) -> None:
    """Change repository NAME; only the options given change.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The repository name.
    url
        New URL.
    credential_secret
        New secret name.
    credential_username
        New HTTPS username.
    known_hosts_file
        New known_hosts file.
    default_ref
        New default ref.
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
        for key, value in {
            "url": url,
            "credential_secret": credential_secret,
            "credential_username": credential_username,
            "ssh_known_hosts": _read_known_hosts(known_hosts_file),
            "default_ref": default_ref,
        }.items()
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
    repo = state.client().repositories.update(name, **changes)
    _output.done(state, f"Updated repository {_output.inline(name)}", repo)


@repo_app.command("remove")
def remove(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete repository NAME and its deploy history.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The repository name.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Delete repository {name} and its deploy history?", yes)
    state.client().repositories.delete(name)
    _output.done(state, f"Deleted repository {_output.inline(name)}")


def register(app: typer.Typer) -> None:
    """Add the `repo` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(repo_app, name="repo")
