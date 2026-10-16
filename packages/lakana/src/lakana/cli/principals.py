"""`lakana principal *`: manage users and service accounts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import confirm, state_of

principal_app = typer.Typer(help="Manage principals.", no_args_is_help=True)


class PrincipalType(StrEnum):
    """The values `--type` accepts."""

    user = "user"
    service_account = "service-account"


@principal_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    principal_type: Annotated[
        PrincipalType, typer.Option("--type", help="user or service-account")
    ],
) -> None:
    """Create principal NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The principal name.
    principal_type
        User or service account.
    """
    state = state_of(ctx)
    api_type = principal_type.value.upper().replace("-", "_")
    principal = state.client().principals.create(name, api_type)
    _output.done(state, f"Created {principal_type.value} {_output.inline(name)}", principal)


@principal_app.command("list")
def list_principals(ctx: typer.Context) -> None:
    """List principals.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().principals.list())


@principal_app.command("get")
def get(ctx: typer.Context, name: str) -> None:
    """Show principal NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The principal name.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().principals.get(name))


@principal_app.command("deactivate")
def deactivate(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Deactivate principal NAME; its credentials stop working.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The principal name.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Deactivate principal {name!r}? Its tokens stop working.", yes)
    state.client().principals.deactivate(name)
    _output.done(state, f"Deactivated principal {_output.inline(name)}")


def register(app: typer.Typer) -> None:
    """Add the `principal` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(principal_app, name="principal")
