"""`lakana group *`: manage groups and their members."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import confirm, state_of

group_app = typer.Typer(help="Manage groups.", no_args_is_help=True)

_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]


@group_app.command("create")
def create(ctx: typer.Context, name: str) -> None:
    """Create group NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The group name.
    """
    state = state_of(ctx)
    _output.done(state, f"Created group {_output.inline(name)}", state.client().groups.create(name))


@group_app.command("list")
def list_groups(ctx: typer.Context) -> None:
    """List groups.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().groups.list())


@group_app.command("delete")
def delete(ctx: typer.Context, name: str, yes: _YES = False) -> None:
    """Delete group NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The group name.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Delete group {name!r}?", yes)
    state.client().groups.delete(name)
    _output.done(state, f"Deleted group {_output.inline(name)}")


@group_app.command("members")
def members(ctx: typer.Context, name: str) -> None:
    """List group NAME's members.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The group name.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().groups.list_members(name))


@group_app.command("add-member")
def add_member(ctx: typer.Context, name: str, principal: str) -> None:
    """Add PRINCIPAL to group NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The group name.
    principal
        The principal name.
    """
    state = state_of(ctx)
    state.client().groups.add_member(name, principal)
    _output.done(state, f"Added {_output.inline(principal)} to group {_output.inline(name)}")


@group_app.command("remove-member")
def remove_member(ctx: typer.Context, name: str, principal: str, yes: _YES = False) -> None:
    """Remove PRINCIPAL from group NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The group name.
    principal
        The principal name.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Remove {principal!r} from group {name!r}?", yes)
    state.client().groups.remove_member(name, principal)
    _output.done(state, f"Removed {_output.inline(principal)} from group {_output.inline(name)}")


def register(app: typer.Typer) -> None:
    """Add the `group` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(group_app, name="group")
