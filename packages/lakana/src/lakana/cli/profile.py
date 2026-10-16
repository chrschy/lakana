"""`lakana profile list|use|remove`: manage the profile file locally."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli import _output, _profiles
from lakana.cli._state import confirm, state_of

profile_app = typer.Typer(help="Manage saved profiles (local only).", no_args_is_help=True)


@profile_app.command("list")
def list_profiles(ctx: typer.Context) -> None:
    """List saved profiles; tokens are never shown.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(
        state, _profiles.list_profiles(state.profile_path()), droppable=["default_environment"]
    )


@profile_app.command("use")
def use(
    ctx: typer.Context,
    name: str,
    env: Annotated[
        str | None, typer.Option("--env", help="Also set its default environment.")
    ] = None,
) -> None:
    """Make NAME the default profile.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The profile.
    env
        A new default environment for it.
    """
    state = state_of(ctx)
    _profiles.use_profile(state.profile_path(), name, env)
    suffix = f" with default environment {env!r}" if env else ""
    _output.done(state, f"Now using profile {name!r}{suffix}")


@profile_app.command("remove")
def remove(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete profile NAME and its token.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The profile.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Remove profile {name!r} and its token?", yes)
    _profiles.remove_profile(state.profile_path(), name)
    _output.done(state, f"Removed profile {name!r}")


def register(app: typer.Typer) -> None:
    """Add the `profile` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(profile_app, name="profile")
