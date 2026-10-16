"""`lakana login`, `lakana logout`, and `lakana whoami`."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Annotated

import typer

import lakana
from lakana._models import Whoami
from lakana.cli import _output, _profiles
from lakana.cli._state import CliError, CliState, state_of


@dataclass(frozen=True)
class LoginResult:
    """What `lakana login` reports; deliberately has no token field.

    Parameters
    ----------
    profile
        The profile written.
    url
        Its platform URL.
    default_environment
        The default environment chosen, if any.
    whoami
        Who the credential authenticates as.
    """

    profile: str
    url: str
    default_environment: str | None
    whoami: Whoami


def _choose_environment(state: CliState, client: lakana.Client, who: Whoami) -> str | None:
    """Offer the visible environments and return the chosen default, if any.

    Preselects a task token's own environment, else `dev`, else the only
    visible environment — but only one that is visible and unprotected, so
    a protected environment is never preselected. A task token whose own
    environment doesn't qualify preselects nothing: it can't act anywhere
    else. The list and prompt go to stderr, keeping stdout for the result.
    Without a TTY nothing is chosen.

    Parameters
    ----------
    state
        The CLI state.
    client
        A client with the verified credential.
    who
        The verified identity.

    Returns
    -------
    str or None
        The environment name, or `None` for no default.

    Raises
    ------
    CliError
        If the user types an environment that isn't visible (exit 2).
    """
    if not state.is_interactive():
        return None
    environments = client.environments.list()
    names = [env.name for env in environments]
    unprotected = [env.name for env in environments if not env.is_protected]
    if who.task_token is not None:
        own = who.task_token.environment
        preselected: str | None = own if own in unprotected else None
    elif "dev" in unprotected:
        preselected = "dev"
    elif len(environments) == 1 and unprotected:
        preselected = unprotected[0]
    else:
        preselected = None
    if names:
        sys.stderr.write("Environments: " + ", ".join(names) + "\n")
    choice = typer.prompt(
        "Default environment (blank for none)",
        default=preselected or "",
        show_default=True,
        err=True,
    ).strip()
    if not choice:
        return None
    if choice not in names and (who.task_token is None or choice != who.task_token.environment):
        raise CliError(f"Unknown environment {choice!r}; visible: {', '.join(names) or 'none'}")
    return choice


def login(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile name to write.")] = None,
    url: Annotated[str | None, typer.Option("--url", help="Platform URL.")] = None,
    token_stdin: Annotated[
        bool, typer.Option("--token-stdin", help="Read the token from stdin (for CI).")
    ] = False,
    env: Annotated[str | None, typer.Option("--env", help="Default environment.")] = None,
) -> None:
    """Verify a credential and save it as a profile.

    Parameters
    ----------
    ctx
        The Typer context.
    profile
        The profile to write; defaults to the global `--profile`, then
        `LAKANA_PROFILE`, then `default`.
    url
        The platform URL; prompted for when omitted.
    token_stdin
        Read the token from stdin instead of a hidden prompt.
    env
        The profile's default environment; chosen interactively when omitted.

    Raises
    ------
    CliError
        For `--token-stdin` without `--url`, or an empty token (exit 2).
    """
    state = state_of(ctx)
    name = profile or state.profile or os.environ.get("LAKANA_PROFILE") or "default"
    base_url = url or state.url
    if token_stdin:
        if not base_url:
            raise CliError("--token-stdin needs --url (stdin can't also answer a prompt)")
        token = sys.stdin.readline().strip()
    else:
        if not base_url:
            base_url = typer.prompt("Lakana URL", err=True).strip()
        token = typer.prompt("Token", hide_input=True, err=True).strip()
    if not token:
        raise CliError("No token given.")
    with state.login_client_factory(base_url, token) as client:
        who = client.whoami()
        environment = env or _choose_environment(state, client, who)
    _profiles.set_profile(
        state.profile_path(), name, url=base_url, token=token, default_environment=environment
    )
    scope = (
        ""
        if who.task_token is None
        else f", task token for {_output.inline(who.task_token.environment)}"
    )
    _output.done(
        state,
        f"Logged in as {_output.inline(who.name)} ({who.type}{scope}) at "
        f"{_output.inline(base_url)}; saved profile {name!r}",
        LoginResult(profile=name, url=base_url, default_environment=environment, whoami=who),
    )


def logout(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile to remove.")] = None,
) -> None:
    """Remove a saved profile (and its token) from the profile file.

    Parameters
    ----------
    ctx
        The Typer context.
    profile
        The profile; defaults to the active one.

    Raises
    ------
    CliError
        If no profile is active or it doesn't exist (exit 2).
    """
    state = state_of(ctx)
    path = state.profile_path()
    name = _profiles.active_profile_name(_profiles.load(path), profile or state.profile)
    if name is None:
        raise CliError("No profile selected; pass --profile NAME")
    _profiles.remove_profile(path, name)
    _output.done(state, f"Removed profile {name!r}")


def whoami(ctx: typer.Context) -> None:
    """Show who the configured credential authenticates as.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().whoami())


def register(app: typer.Typer) -> None:
    """Add `login`, `logout` and `whoami` to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.command("login")(login)
    app.command("logout")(logout)
    app.command("whoami")(whoami)
