"""`lakana env *`: manage environments."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

import typer

from lakana.cli import _output
from lakana.cli._state import CliError, confirm, parse_key_values, state_of

env_app = typer.Typer(help="Manage environments.", no_args_is_help=True)

_SCHEMES = ("file", "s3", "abfs")
_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]
_OPTIONS = Annotated[
    list[str] | None,
    typer.Option(
        "--option",
        help=(
            "Storage option key=value (repeatable). Values land in shell history; "
            "prefer --secret-ref for credentials."
        ),
    ),
]
_STORAGE_ROOT_HELP = "e.g. file:///data or s3://bucket/lake"


@env_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    storage_root: Annotated[str, typer.Option("--storage-root", help=_STORAGE_ROOT_HELP)],
    option: _OPTIONS = None,
) -> None:
    """Create environment NAME (with bronze/silver/gold namespaces).

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment name.
    storage_root
        Its storage root URI.
    option
        Storage options as key=value.

    Raises
    ------
    CliError
        If the root has no supported scheme (exit 2).
    """
    state = state_of(ctx)
    scheme = urlsplit(storage_root).scheme
    if scheme not in _SCHEMES:
        raise CliError(
            f"--storage-root must start with one of {', '.join(s + '://' for s in _SCHEMES)}; "
            f"got {storage_root!r}"
        )
    environment = state.client().environments.create(
        name,
        storage={
            "scheme": scheme,
            "root": storage_root,
            "options": parse_key_values(option, "--option", sensitive=True),
        },
    )
    _output.done(
        state,
        f"Created environment {_output.inline(name)} at {_output.inline(storage_root)}",
        environment,
    )


@env_app.command("list")
def list_environments(ctx: typer.Context) -> None:
    """List environments you can see.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(
        state,
        state.client().environments.list(),
        columns=["name", "storage_root", "is_protected"],
        droppable=["storage_root"],
    )


@env_app.command("get")
def get(ctx: typer.Context, name: str) -> None:
    """Show environment NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().environments.get(name))


@env_app.command("delete")
def delete(ctx: typer.Context, name: str, yes: _YES = False) -> None:
    """Delete environment NAME.

    Refused (409) while any repository is deployed to it: run
    `lakana undeploy REPO --env NAME` for each first. Refused while protected.
    Data in object storage is left alone.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Delete environment {name!r}?", yes)
    state.client().environments.delete(name)
    _output.done(state, f"Deleted environment {_output.inline(name)}")


@env_app.command("set-storage")
def set_storage(
    ctx: typer.Context,
    name: str,
    option: _OPTIONS = None,
    secret_ref: Annotated[
        str | None, typer.Option("--secret-ref", help="Secret holding credentials.")
    ] = None,
) -> None:
    """Change environment NAME's storage options or credentials secret.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    option
        New storage options as key=value.
    secret_ref
        The name of a secret holding storage credentials.

    Raises
    ------
    CliError
        If nothing would change (exit 2).
    """
    state = state_of(ctx)
    if not option and secret_ref is None:
        raise CliError("Nothing to change: pass --option and/or --secret-ref")
    environment = state.client().environments.update_storage(
        name,
        options=parse_key_values(option, "--option", sensitive=True) if option else None,
        secret_ref=secret_ref,
    )
    _output.done(state, f"Updated storage for environment {_output.inline(name)}", environment)


@env_app.command("protect")
def protect(ctx: typer.Context, name: str) -> None:
    """Protect environment NAME: lock writes down to deployed runs.

    A protected environment refuses every write outside runs of the deployed
    code (SQL, SDK, CLI and Iceberg REST clients alike) whatever the caller's
    grants; bind a run principal with `lakana deployments set-run-as`. A holder
    of MANAGE_ENVIRONMENT can make one audited write with `--break-glass
    --reason`. It also refuses deletion and storage changes.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    """
    state = state_of(ctx)
    environment = state.client().environments.set_protection(name, True)
    _output.done(state, f"Protected environment {_output.inline(name)}", environment)


@env_app.command("unprotect")
def unprotect(ctx: typer.Context, name: str, yes: _YES = False) -> None:
    """Remove protection from environment NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Remove protection from environment {name!r}?", yes)
    environment = state.client().environments.set_protection(name, False)
    _output.done(state, f"Unprotected environment {_output.inline(name)}", environment)


def register(app: typer.Typer) -> None:
    """Add the `env` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(env_app, name="env")
